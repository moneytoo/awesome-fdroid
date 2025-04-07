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

import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from urllib.parse import urlparse
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv
from google_play_scraper.exceptions import NotFoundError

REPO_NAME_FDROID = "F-Droid"
REPO_NAME_FDROID_ARCHIVE = "F-Droid (Archive)"
REPO_NAME_IZZY = "IzzyOnDroid"

DIR_GITHUB = 'work/github'
DIR_GITLAB = 'work/gitlab'
DIR_CODEBERG = 'work/codeberg'
DIR_ICONS = "icons"
DIR_FDROIDDATA = 'work/fdroiddata'
DIR_METRICS = 'work/metrics'

FILE_REPO_FDROID = "work/fdroid.xml"
FILE_REPO_FDROID_ARCHIVE = "work/fdroid-archive.xml"
FILE_REPO_IZZY = "work/izzy.xml"
FILE_VERSIONS_PKL = 'work/versions.pkl'

URL_REPO_FDROID = "https://f-droid.org/repo"
URL_REPO_FDROID_ARCHIVE = "https://f-droid.org/archive"
URL_REPO_IZZY = "https://apt.izzysoft.de/fdroid/repo"

OUTPUT = "index.html"

metrics = {}
versions = {}
no_icon = None
google_missing = None

load_dotenv()

API_TOKEN_GITHUB = os.getenv('API_TOKEN_GITHUB')
API_TOKEN_GITLAB = os.getenv('API_TOKEN_GITLAB')

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
    response = requests.get(repo_url)
    with open(path, 'wb') as file:
        file.write(response.content)

def fetch_stats():
    for dir in [DIR_GITHUB, DIR_GITLAB, DIR_CODEBERG]:
        os.makedirs(dir, exist_ok=True)
    for repo in [FILE_REPO_FDROID, FILE_REPO_FDROID_ARCHIVE, FILE_REPO_IZZY]:
        fetch_stats_for_repo(repo)

def fetch_stats_for_repo(repo_file):
    github_repo_pattern = re.compile(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)(?:/.*)?$')
    gitlab_repo_pattern = re.compile(r'^https?://(?:www\.)?gitlab\.com/([^/]+)/([^/]+)(?:/.*)?$')
    codeberg_repo_pattern = re.compile(r'^https?://(?:www\.)?codeberg\.org/([^/]+)/([^/]+)(?:/.*)?$')

    for application in ET.parse(repo_file).getroot().findall('application'):
        pkg = application.find('id').text
        source = application.find('source').text
        if source is None:
            continue
        match_github = github_repo_pattern.match(source)
        if match_github:
            owner, repo = match_github.groups()
            headers = {
                "Authorization": f"token {API_TOKEN_GITHUB}",
                "Accept": "application/vnd.github.v3+json"
            }
            fetch_stats_save(f"https://api.github.com/repos/{owner}/{repo}", headers, pkg, DIR_GITHUB)
            continue
        match_gitlab = gitlab_repo_pattern.match(source)
        if match_gitlab:
            owner, repo = match_gitlab.groups()
            headers = {
                "PRIVATE-TOKEN": f"{API_TOKEN_GITLAB}"
            }
            fetch_stats_save(f"https://gitlab.com/api/v4/projects/{owner}%2F{repo}", headers, pkg, DIR_GITLAB)
            continue
        match_codeberg = codeberg_repo_pattern.match(source)
        if match_codeberg:
            owner, repo = match_codeberg.groups()
            fetch_stats_save(f"https://codeberg.org/api/v1/repos/{owner}/{repo}", None, pkg, DIR_CODEBERG)
            continue

def fetch_stats_save(url, headers, pkg, dir):
    save_path = os.path.join(dir, f"{pkg}.json")
    if os.path.exists(save_path):
        return
    data = requests.get(url, headers=headers).json()
    with open(save_path, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=4)

def fetch_icons():
    os.makedirs(DIR_ICONS, exist_ok=True)
    no_icon_file = Path("work/iconless.pkl")
    global no_icon
    no_icon = pickle.loads(no_icon_file.read_bytes()) if no_icon_file.exists() else set()
    fetch_icons_from_repo(FILE_REPO_FDROID, URL_REPO_FDROID)
    fetch_icons_from_repo(FILE_REPO_FDROID_ARCHIVE, URL_REPO_FDROID_ARCHIVE)
    fetch_icons_from_repo(FILE_REPO_IZZY, URL_REPO_IZZY)
    no_icon_file.write_bytes(pickle.dumps(no_icon))

def fetch_icons_from_repo(repo_file, repo_url):
    for application in ET.parse(repo_file).getroot().findall('application'):
        pkg = application.find('id').text
        if os.path.exists(f"{DIR_ICONS}/{pkg}.png"):
            continue
        global no_icon
        if pkg in no_icon:
            continue
        if application.find('icon') is not None:
            ico = application.find('icon').text
            url = f"{repo_url}/icons/{ico}"
        else:
            url = f"{repo_url}/{pkg}/en-US/icon.png"
        fetch_icon(url, pkg)

def fetch_icon(url, pkg):
    if "f-droid.org" not in urlparse(url).netloc:
        time.sleep(7)
    path = f"{DIR_ICONS}/{pkg}.png"
    response = requests.get(url)
    if response.status_code == 200:
        with open(path, 'wb') as file:
            file.write(response.content)
    else:
        global no_icon
        no_icon.add(pkg)

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
        "fdroid_downloads": fdroid_downloads,
        "google_downloads": google_downloads
    }

def get_stats(pkg):
    github_file = os.path.join(DIR_GITHUB, f"{pkg}.json")
    gitlab_file = os.path.join(DIR_GITLAB, f"{pkg}.json")
    codeberg_file = os.path.join(DIR_CODEBERG, f"{pkg}.json")
    for file_path, source_type in [(github_file, 'github'), (gitlab_file, 'gitlab'), (codeberg_file, 'codeberg')]:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
                match source_type:
                    case 'github':
                        stars = data.get('stargazers_count', None)
                    case 'gitlab':
                        stars = data.get('star_count', None)
                    case 'codeberg':
                        stars = data.get('stars_count', None)
                    case _:
                        stars = None
                status = "Archived" if data.get('archived', False) else None
                issues = data.get('open_issues_count', None)
                language = data.get('language', None)
                return stars, status, issues, language
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
        subprocess.run(["git", "clone", url, DIR_FDROIDDATA])

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
    for dir in ['http02', 'http03']:
        path = f"{DIR_METRICS}/{dir}"
        if is_path_fresh(f"{path}/index.json"):
            return
        os.makedirs(path, exist_ok=True)
        url = f"https://fdroid.gitlab.io/metrics/{dir}.fdroid.net/index.json"
        response = requests.get(url)
        with open(f"{path}/index.json", 'wb') as file:
            file.write(response.content)
        index = response.json()
        for file_name in index:
            if os.path.exists(f"{path}/{file_name}"):
                continue
            file_url = f"https://fdroid.gitlab.io/metrics/{dir}.fdroid.net/{file_name}"
            response = requests.get(file_url)
            with open(f"{path}/{file_name}", 'wb') as file:
                file.write(response.content)

def process_metrics():
    global metrics
    global versions
    for dir in ['http02', 'http03']:
        path = f"{DIR_METRICS}/{dir}"
        for filename in os.listdir(path):
            if filename == 'index.json':
                continue
            full_path = os.path.join(path, filename)
            with open(full_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
                paths = data.get('paths')
                for value in paths:
                    if value.startswith('/repo/') and value.endswith('.apk'):
                        pattern = re.compile(r"^/repo/(.+)_(\d+)\.apk$")
                        match = pattern.match(value)
                        if match:
                            package, versionCode = match.groups()
                            versionCode = int(versionCode)
                            hits = paths.get(value).get('hits')
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
    path = f"work/google/{package}.json"
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return data.get('realInstalls', None)
    return None

def fetch_google_play():
    os.makedirs('work/google', exist_ok=True)
    fetch_google_play_apps_from_repo(FILE_REPO_FDROID)
    fetch_google_play_apps_from_repo(FILE_REPO_FDROID_ARCHIVE)
    fetch_google_play_apps_from_repo(FILE_REPO_IZZY)

def fetch_google_play_apps_from_repo(repo_file):
    google_missing_file = Path("work/google_missing.pkl")
    global google_missing
    google_missing = pickle.loads(google_missing_file.read_bytes()) if google_missing_file.exists() else set()

    for application in ET.parse(repo_file).getroot().findall('application'):
        pkg = application.find('id').text
        path = f"work/google/{pkg}.json"
        if os.path.exists(path) or pkg in google_missing:
            continue
        #time.sleep(random.uniform(5, 10))
        try:
            result = google_play_scraper.app(pkg)
            with open(path, 'w') as file:
                json.dump(result, file, indent=4)
        except NotFoundError as e:
            google_missing.add(pkg)
        google_missing_file.write_bytes(pickle.dumps(google_missing))


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
