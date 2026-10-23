import os
import json
import re
import requests
import time
import pickle
import subprocess
import yaml
import google_play_scraper
import random
import zlib

import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv
from google_play_scraper.exceptions import NotFoundError, ExtraHTTPError

REPO_NAME_FDROID = "F-Droid"
REPO_NAME_FDROID_ARCHIVE = "F-Droid (Archive)"
REPO_NAME_IZZY = "IzzyOnDroid"

DIR_ICONS = "icons"
DIR_FDROIDDATA = 'work/fdroiddata'
DIR_STATE = 'state'
DIR_METRICS = 'state/metrics'

FILE_REPO_FDROID = "work/fdroid.xml"
FILE_REPO_FDROID_ARCHIVE = "work/fdroid-archive.xml"
FILE_REPO_IZZY = "work/izzy.xml"
FILE_VERSIONS_PKL = 'work/versions.pkl'

URL_REPO_FDROID = "https://f-droid.org/repo"
URL_REPO_FDROID_ARCHIVE = "https://f-droid.org/archive"
URL_REPO_IZZY = "https://apt.izzysoft.de/fdroid/repo"

OUTPUT = "index.html"

TTL_STATS = 7
TTL_GOOGLE = 30
TTL_MISSING = 30

BUDGET_STATS = {'github': 1000, 'gitlab': 200, 'codeberg': 100}
PAUSE_STATS = {'github': 0, 'gitlab': 0.5, 'codeberg': 2}
BUDGET_GOOGLE = 300
BUDGET_ICONS = {'f-droid.org': 300, 'apt.izzysoft.de': 50}

metrics = {}
versions = {}
stats = {}
google = {}

load_dotenv()

API_TOKEN_GITHUB = os.getenv('API_TOKEN_GITHUB')
API_TOKEN_GITLAB = os.getenv('API_TOKEN_GITLAB')
API_TOKEN_CODEBERG = os.getenv('API_TOKEN_CODEBERG')

def load_state(name):
    path = Path(DIR_STATE, f"{name}.json")
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def save_state(name, state):
    os.makedirs(DIR_STATE, exist_ok=True)
    lines = [f"{json.dumps(pkg)}: {json.dumps(state[pkg], sort_keys=True)}" for pkg in sorted(state)]
    Path(DIR_STATE, f"{name}.json").write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding='utf-8')

def timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def expires(pkg, record, days):
    jitter = 0.8 + 0.4 * zlib.crc32(pkg.encode()) / 2**32
    return datetime.fromisoformat(record['fetched']) + timedelta(days=days * jitter)

def due(state, pkgs, ttl, budget):
    now = datetime.now(timezone.utc)
    pending = []
    for pkg in pkgs:
        record = state.get(pkg)
        if record is None:
            pending.append((datetime.min.replace(tzinfo=timezone.utc), pkg))
            continue
        expiry = expires(pkg, record, ttl if record['status'] == 200 else TTL_MISSING)
        if expiry <= now:
            pending.append((expiry, pkg))
    return [pkg for _, pkg in sorted(pending)[:budget]]

def is_throttled(response):
    if response.status_code in (401, 429):
        return True
    return response.status_code == 403 and (response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers)

def fetch_stats_for_repos():
    fetch_fdroid_repo(f"{URL_REPO_FDROID}/index.xml", FILE_REPO_FDROID)
    fetch_fdroid_repo(f"{URL_REPO_FDROID_ARCHIVE}/index.xml", FILE_REPO_FDROID_ARCHIVE)
    fetch_fdroid_repo(f"{URL_REPO_IZZY}/index.xml", FILE_REPO_IZZY)

def is_path_fresh(path):
    if os.path.exists(path):
        if (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))) < timedelta(hours=4):
            return True
    return False

def fetch_fdroid_repo(repo_url, path):
    if is_path_fresh(path):
        return
    response = requests.get(repo_url, timeout=300)
    response.raise_for_status()
    with open(path, 'wb') as file:
        file.write(response.content)

def fetch_stats():
    patterns = {
        'github': re.compile(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)(?:/.*)?$'),
        'gitlab': re.compile(r'^https?://(?:www\.)?gitlab\.com/([^/]+)/([^/]+)(?:/.*)?$'),
        'codeberg': re.compile(r'^https?://(?:www\.)?codeberg\.org/([^/]+)/([^/]+)(?:/.*)?$')
    }
    sources = {name: {} for name in patterns}
    for repo_file in [FILE_REPO_FDROID, FILE_REPO_FDROID_ARCHIVE, FILE_REPO_IZZY]:
        for application in ET.parse(repo_file).getroot().findall('application'):
            pkg = application.find('id').text
            source = application.find('source').text
            if source is None:
                continue
            for name, pattern in patterns.items():
                match = pattern.match(source)
                if match:
                    sources[name].setdefault(pkg, match.groups())
                    break
    for name, repos in sources.items():
        stats[name] = {pkg: record for pkg, record in load_state(name).items() if pkg in repos}
        fetch_stats_for_platform(name, repos, stats[name])
        save_state(name, stats[name])

def stats_request(name, owner, repo):
    match name:
        case 'github':
            return f"https://api.github.com/repos/{owner}/{repo}", {
                "Authorization": f"token {API_TOKEN_GITHUB}",
                "Accept": "application/vnd.github.v3+json"
            }
        case 'gitlab':
            return f"https://gitlab.com/api/v4/projects/{owner}%2F{repo}", {
                "PRIVATE-TOKEN": f"{API_TOKEN_GITLAB}"
            }
        case 'codeberg':
            headers = {"Authorization": f"token {API_TOKEN_CODEBERG}"} if API_TOKEN_CODEBERG else None
            return f"https://codeberg.org/api/v1/repos/{owner}/{repo}", headers

def fetch_stats_for_platform(name, repos, state):
    stars_key = {'github': 'stargazers_count', 'gitlab': 'star_count', 'codeberg': 'stars_count'}[name]
    for pkg in due(state, repos, TTL_STATS, BUDGET_STATS[name]):
        url, headers = stats_request(name, *repos[pkg])
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            print(f"{name}: {pkg}: {e}")
            continue
        if is_throttled(response):
            print(f"::warning::{name}: stopped at {pkg} with HTTP {response.status_code}")
            break
        if response.status_code >= 500:
            continue
        if response.status_code == 200:
            data = response.json()
            state[pkg] = {
                "stars": data.get(stars_key),
                "archived": data.get('archived', False),
                "issues": data.get('open_issues_count'),
                "language": data.get('language'),
                "status": 200,
                "fetched": timestamp()
            }
        else:
            state[pkg] = {"status": response.status_code, "fetched": timestamp()}
        time.sleep(PAUSE_STATS[name])

def fetch_icons():
    os.makedirs(DIR_ICONS, exist_ok=True)
    icons = {}
    for repo_file, repo_url in [(FILE_REPO_FDROID, URL_REPO_FDROID), (FILE_REPO_FDROID_ARCHIVE, URL_REPO_FDROID_ARCHIVE), (FILE_REPO_IZZY, URL_REPO_IZZY)]:
        for application in ET.parse(repo_file).getroot().findall('application'):
            pkg = application.find('id').text
            if pkg in icons:
                continue
            if application.find('icon') is not None:
                ico = application.find('icon').text
                url = f"{repo_url}/icons/{ico}"
            else:
                url = f"{repo_url}/{pkg}/en-US/icon.png"
            icons[pkg] = (url, application.find('lastupdated').text)
    state = {pkg: record for pkg, record in load_state('icons').items() if pkg in icons}
    budget = dict(BUDGET_ICONS)
    for pkg, (url, lastupdated) in icons.items():
        record = state.get(pkg)
        exists = os.path.exists(f"{DIR_ICONS}/{pkg}.png")
        if record is None and exists:
            state[pkg] = {"lastupdated": lastupdated, "status": 200, "fetched": timestamp()}
            continue
        if not is_icon_stale(pkg, record, lastupdated, exists):
            continue
        host = urlparse(url).netloc
        if budget.get(host, 0) <= 0:
            continue
        budget[host] -= 1
        fetch_icon(url, pkg, lastupdated, state)
    save_state('icons', state)

def is_icon_stale(pkg, record, lastupdated, exists):
    if record is None or record['lastupdated'] != lastupdated:
        return True
    if record['status'] == 200:
        return not exists
    return expires(pkg, record, TTL_MISSING) <= datetime.now(timezone.utc)

def fetch_icon(url, pkg, lastupdated, state):
    if "f-droid.org" not in urlparse(url).netloc:
        time.sleep(7)
    path = Path(DIR_ICONS, f"{pkg}.png")
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"icon: {pkg}: {e}")
        return
    if response.status_code == 429 or response.status_code >= 500:
        return
    if response.status_code == 200 and (not path.exists() or path.read_bytes() != response.content):
        path.write_bytes(response.content)
    state[pkg] = {"lastupdated": lastupdated, "status": response.status_code, "fetched": timestamp()}

def build_table_data():
    data = []
    added_pkgs = set()

    def add_apps(file, repo_name):
        for app in ET.parse(file).getroot().findall('application'):
            pkg = app.find('id').text
            if pkg not in added_pkgs:
                row = build_row_data(app, repo_name)
                data.append(row)
                if repo_name == REPO_NAME_FDROID:
                    added_pkgs.add(pkg)

    add_apps(FILE_REPO_FDROID, REPO_NAME_FDROID)
    add_apps(FILE_REPO_IZZY, REPO_NAME_IZZY)
    add_apps(FILE_REPO_FDROID_ARCHIVE, REPO_NAME_FDROID_ARCHIVE)

    data.sort(key=lambda x: (x['stars'] is not None, x['stars']), reverse=True)

    return data

def sdk_to_version(sdk):
    mapping = {
        1: 1,
        2: 1.1,
        3: 1.5,
        4: 1.6,
        5: 2.0,
        6: 2.0,
        7: 2.1,
        8: 2.2,
        9: 2.3,
        10: 2.3,
        11: 3.0,
        12: 3.1,
        13: 3.2,
        14: 4.0,
        15: 4.0,
        16: 4.1,
        17: 4.2,
        18: 4.3,
        19: 4.4,
        20: 4.4,
        21: 5.0,
        22: 5.1,
        23: 6.0,
        24: 7.0,
        25: 7.1,
        26: 8.0,
        27: 8.1,
        28: 9,
        29: 10,
        30: 11,
        31: 12,
        32: 12
    }
    if sdk in mapping:
        return mapping[sdk]
    else:
        return sdk - 20

def build_row_data(application, repo):
    pkg = application.find('id').text

    name = application.find('name').text
    summary = application.find('summary').text
    added = application.find('added').text
    lastupdated = application.find('lastupdated').text
    source = application.find('source').text
    if source is not None:
        source = source.removeprefix('https://').removeprefix('http://')
    license = application.find('license').text
    category = application.find('category').text
    sdk = None
    minimum = None
    for package in application.findall('package'):
        sdkver = package.find('sdkver')
        if sdkver is not None:
            i = int(sdkver.text)
            if sdk is None or i < sdk:
                sdk = i
    if sdk is not None:
        minimum = sdk_to_version(sdk)
    icon = f"icons/{pkg}.png"
    if not os.path.exists(icon):
        icon = None
    fdroid_downloads = get_metrics_count(pkg)
    google_downloads = get_google_downloads(pkg)

    stars, status, issues, language = get_stats(pkg)

    return {
        "icon": icon,
        "name": name,
        "package": pkg,
        "source": source,
        "license": license,
        "category": category,
        "stars": stars,
        "status": status,
        "summary": summary,
        "added": added,
        "lastupdated": lastupdated,
        "issues": issues,
        "language": language,
        "repo": repo,
        "minimum": minimum,
        "fdroid_downloads": fdroid_downloads,
        "google_downloads": google_downloads
    }

def get_stats(pkg):
    for name in ['github', 'gitlab', 'codeberg']:
        record = stats.get(name, {}).get(pkg)
        if record is not None:
            status = "Archived" if record.get('archived', False) else None
            return record.get('stars'), status, record.get('issues'), record.get('language')
    return None, None, None, None

def build_html(data):
    environment = Environment(loader=FileSystemLoader("template/"))
    template = environment.get_template("handsontable.html")
    rendered_html = template.render(data=json.dumps(data))
    with open(OUTPUT, 'w', encoding='utf-8') as file:
        file.write(rendered_html)

def fetch_fdroiddata():
    url = 'https://gitlab.com/fdroid/fdroiddata.git/'
    if os.path.isdir(os.path.join(DIR_FDROIDDATA, ".git")):
        subprocess.run(["git", "-C", DIR_FDROIDDATA, "pull"])
    else:
        subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", url, DIR_FDROIDDATA])
        subprocess.run(["git", "-C", DIR_FDROIDDATA, "sparse-checkout", "set", "--no-cone", "/metadata/*.yml"])

def load_data():
    global versions
    if is_path_fresh(FILE_VERSIONS_PKL):
        with open(FILE_VERSIONS_PKL, "rb") as file:
            versions = pickle.load(file)
    else:
        path = f"{DIR_FDROIDDATA}/metadata"
        for filename in os.listdir(path):
            full_path = os.path.join(path, filename)
            if os.path.isfile(full_path):
                pkg = Path(filename).stem
                with open(full_path, 'r', encoding='utf-8') as file:
                    yml = yaml.safe_load(file)
                    versions[pkg] = {}
                    for build in yml.get("Builds", []):
                        versions[pkg][build.get('versionCode')] = build.get('versionName')
        with open(FILE_VERSIONS_PKL, "wb") as file:
            pickle.dump(versions, file)


def fetch_metrics():
    pattern = re.compile(r"^/repo/(.+)_(\d+)\.apk$")
    for dir in ['http02', 'http03']:
        path = f"{DIR_METRICS}/{dir}"
        os.makedirs(path, exist_ok=True)
        url = f"https://fdroid.gitlab.io/metrics/{dir}.fdroid.net"
        index = requests.get(f"{url}/index.json", timeout=60).json()
        for file_name in index:
            if os.path.exists(f"{path}/{file_name}"):
                continue
            if datetime.now() - datetime.strptime(Path(file_name).stem, '%Y-%m-%d') < timedelta(days=14):
                continue
            response = requests.get(f"{url}/{file_name}", timeout=300)
            if response.status_code != 200:
                continue
            hits = {}
            for value, entry in response.json().get('paths', {}).items():
                match = pattern.match(value)
                if match:
                    package, versionCode = match.groups()
                    hits.setdefault(package, {})[versionCode] = entry.get('hits')
            if hits:
                with open(f"{path}/{file_name}", 'w', encoding='utf-8') as file:
                    json.dump(hits, file, sort_keys=True, separators=(',', ':'))

def process_metrics():
    global metrics
    global versions
    for dir in ['http02', 'http03']:
        path = f"{DIR_METRICS}/{dir}"
        for filename in os.listdir(path):
            full_path = os.path.join(path, filename)
            with open(full_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
                for package, codes in data.items():
                    for versionCode, hits in codes.items():
                        versionCode = int(versionCode)
                        if package not in metrics:
                            metrics[package] = {'hits': 0, 'versionCodes': set(), 'versionNames': set()}
                        if package in versions:
                            if versionCode in versions[package]:
                                versionName = versions[package][versionCode]
                                if versionName is not None:
                                    metrics[package]["hits"] += hits
                                    metrics[package]["versionNames"].add(versionName)

def get_metrics_count(package):
    global metrics
    if package in metrics:
        downloads = metrics[package]["hits"]
        versionNames = len(metrics[package]['versionNames'])
        if downloads == 0 or versionNames == 0:
            return None
        average_downloads_per_version = downloads // versionNames
        return average_downloads_per_version

def get_google_downloads(package):
    record = google.get(package)
    if record is not None:
        return record.get('installs')
    return None

def fetch_google_play():
    global google
    pkgs = {}
    for repo_file in [FILE_REPO_FDROID, FILE_REPO_FDROID_ARCHIVE, FILE_REPO_IZZY]:
        for application in ET.parse(repo_file).getroot().findall('application'):
            pkgs.setdefault(application.find('id').text)
    google = {pkg: record for pkg, record in load_state('google').items() if pkg in pkgs}
    for pkg in due(google, pkgs, TTL_GOOGLE, BUDGET_GOOGLE):
        time.sleep(random.uniform(3, 6))
        try:
            result = google_play_scraper.app(pkg)
            google[pkg] = {"installs": result.get('realInstalls'), "status": 200, "fetched": timestamp()}
        except NotFoundError:
            google[pkg] = {"status": 404, "fetched": timestamp()}
        except ExtraHTTPError as e:
            print(f"::warning::google: stopped at {pkg}: {e}")
            break
        except Exception as e:
            print(f"google: {pkg}: {e}")
    save_state('google', google)


fetch_fdroiddata()
fetch_metrics()
fetch_stats_for_repos()
fetch_icons()
fetch_stats()
fetch_google_play()

load_data()
process_metrics()

table_data = build_table_data()
build_html(table_data)
