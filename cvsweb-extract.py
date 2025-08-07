"""
cvsweb-extract.py

Politely scrape archive.org's CVSweb mirror of oss.sgi.com.
Currently, only the latest version of each file is downloaded.

NOTE: Please use this script sparingly! And donate to archive.org
if you have the means.

All graphics stuff from oss.sgi.com is *already mirrored* on Github, and far faster to download:

https://github.com/sgi-demos/ogl-sample
https://github.com/sgi-demos/sgi-inventor
https://github.com/sgi-demos/sgi-performer

Publishng this script as an example of mirroring files from a CVSweb site, where
the original CVS repository is no longer available.

"""

# Configure full URL to mirror here:
FULL_URL = "https://web.archive.org/web/20171010115113/http://oss.sgi.com/cgi-bin/cvsweb.cgi/projects/ogl-sample/"
#FULL_URL = "https://web.archive.org/web/20171010104743/http://oss.sgi.com/cgi-bin/cvsweb.cgi/inventor/"
#FULL_URL = "https://web.archive.org/web/20171010104701/http://oss.sgi.com/cgi-bin/cvsweb.cgi/performer/"

import os
import re
import requests # For making HTTP requests
from bs4 import BeautifulSoup # For parsing HTML
import time # For polite scraping
from urllib.parse import urljoin, urlparse, unquote, quote
from collections import deque # For an efficient queue/stack

CHECKOUT_PREFIX = "~checkout~"
REQUEST_DELAY_SECONDS = 0.5
# identify archive.org replay URLs and capture parts
WAYBACK_URL_PATTERN = re.compile(r"^(https?://web\.archive\.org/web/(\d{14}))(if_)?(/.*)$")


# --- Helper to create a session ---
def get_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 CVSWeb-Snapshot-Iterative/1.3' # Version from user's uploaded file
    })
    return session

SESSION = get_session()

def fetch_page_content(url):
    print(f"Fetching page:    {url}")
    try:
        response = SESSION.get(url, timeout=30)
        response.raise_for_status()
        time.sleep(REQUEST_DELAY_SECONDS)
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"ERROR fetching page {url}: {e}")
        return None

"""
Transforms a Wayback Machine URL to its 'if_' variant for potentially raw content.
Example: /web/TIMESTAMP/http://... -> /web/TIMESTAMPif_/http://...
"""
def get_wayback_raw_content_url(original_wayback_url):
    match = WAYBACK_URL_PATTERN.match(original_wayback_url)
    if match:
        base_wb_url_with_ts = match.group(1) # e.g. https://web.archive.org/web/20171027070355
        already_iframe_version = match.group(3) # This will be 'if_' if already there
        original_content_url_part = match.group(4) # e.g. /http://oss.sgi.com/...

        if already_iframe_version: # It's already the iframe version
            return original_wayback_url
        else:
            # Construct the iframe version URL
            return f"{base_wb_url_with_ts}if_{original_content_url_part}"
    return original_wayback_url # Return original if not a recognized Wayback pattern or already raw

"""
Fetch file content from a checkout URL
"""    
def fetch_file_content_checkout(cgi_base_url, latest_revision, path_for_download_url):
    quoted_repo_relative_path_for_file = "/".join([quote(part) for part in path_for_download_url.split('/')])
    file_download_url = f"{cgi_base_url.rstrip('/')}/{CHECKOUT_PREFIX.strip('/')}/{quoted_repo_relative_path_for_file}?rev={latest_revision}"
    file_download_url = get_wayback_raw_content_url(file_download_url) # Transform to Wayback raw content URL if applicable  
    print(f"Downloading ~checkout~ URL: {file_download_url}") 
    try:
        response = SESSION.get(file_download_url, timeout=60, allow_redirects=True)
        response.raise_for_status()
        time.sleep(REQUEST_DELAY_SECONDS)
        return file_download_url, response.content
    except requests.exceptions.RequestException as e:
        print(f"ERROR downloading url: {e}")
        return file_download_url, None

"""
Fetch file content from a markup URL within <pre> tags.
"""
def fetch_file_content_markup(cgi_base_url, latest_revision, path_for_download_url):
    quoted_repo_relative_path_for_file = "/".join([quote(part) for part in path_for_download_url.split('/')])
    markup_view_url = f"{cgi_base_url.rstrip('/')}/{quoted_repo_relative_path_for_file}?rev={latest_revision}&content-type=text/x-cvsweb-markup"
    print(f"Downloading markup URL: {markup_view_url}")

    try:
        response = SESSION.get(markup_view_url, timeout=60, allow_redirects=True)
        response.raise_for_status() # Check for HTTP errors on markup page
        time.sleep(REQUEST_DELAY_SECONDS)
        html_content_markup = response.text # Use .text as we'll parse with BeautifulSoup

        soup_markup = BeautifulSoup(html_content_markup, 'html.parser')
        content_pre_tag = None
        hr_tags = soup_markup.find_all('hr', noshade=True)
        if len(hr_tags) >= 2:
            # The content <pre> is the direct next sibling of the second <hr>
            pre_candidate = hr_tags[1].find_next_sibling('pre')
            if pre_candidate:
                content_pre_tag = pre_candidate
    
        if not content_pre_tag: # Fallback if the hr logic didn't work (e.g. different page structure)
            all_pre_tags = soup_markup.find_all('pre')
            if all_pre_tags:
                # This heuristic might need refinement if there are multiple <pre> tags.
                # Often the longest <pre> or the last one might be the file content.
                # For the example given, it's the last one.
                content_pre_tag = all_pre_tags[-1] 
        
        if content_pre_tag:
            file_text = content_pre_tag.get_text()
            return markup_view_url, file_text.encode('utf-8') # Return as bytes
        else:
            print(f"      ERROR: fetched markup URL but no good <pre> tag in {markup_view_url}.")
            return markup_view_url, None

    except requests.exceptions.RequestException as e:
        print(f"ERROR downloading URL: {e}")
        return markup_view_url, None

"""
Sanitizes a given name for illegal filesystem characters
"""
def sanitize_filename_for_illegal_chars(name): # Renamed to be more specific about its purpose now
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name)

"""
Iteratively fetches the latest version of files and subdirectories using a queue.
"""
def fetch_latest_snapshot(full_url):

    cgi_base_url = full_url.split('cvsweb.cgi')[0]+'cvsweb.cgi'
    remote_path = full_url.split('cvsweb.cgi')[1].lstrip('/') # works with or without trailing /
    local_output_dir = remote_path.replace('/','-').rstrip('-')

    print(f"CVSweb CGI base URL: {cgi_base_url}")
    print(f"Remote path: {remote_path}")
    print(f"Local output dir: {local_output_dir}")

    if not os.path.exists(local_output_dir):
        try:
            os.makedirs(local_output_dir)
            print(f"Created base output directory: {local_output_dir}")
        except OSError as e:
            print(f"ERROR creating base output directory {local_output_dir}: {e}")
            exit()

    # The initial URL to fetch (directory listing page) is cgi_base_url + / + remote_path
    if not remote_path[-1] == '/':
        remote_path += '/'
    initial_dir_view_url = f"{cgi_base_url.rstrip('/')}/{remote_path.lstrip('/')}"

    queue = deque([(initial_dir_view_url, remote_path, "")]) 
    visited_urls = set()
    file_errors = []
    saved_dirs = saved_files = 0
    skipped_dirs = skipped_files = 0

    while queue:
        current_dir_view_url, current_full_repo_path, current_local_rel_path = queue.popleft()

        if current_dir_view_url in visited_urls:
            print(f"Already visited URL: {current_dir_view_url}")
            continue
        visited_urls.add(current_dir_view_url)

        print(f"\nVisiting URL:     {current_dir_view_url}")
        print(f"Full repo path:   {current_full_repo_path}")
        print(f"Local save path:  {current_local_rel_path}")

        html_content = fetch_page_content(current_dir_view_url)
        if not html_content:
            continue

        current_local_full_path = os.path.join(local_output_dir, current_local_rel_path)
        if not os.path.exists(current_local_full_path): 
            try:
                os.makedirs(current_local_full_path)
                print(f"Created local dir: {current_local_full_path}")
                saved_dirs += 1
            except OSError as e:
                print(f"ERROR creating dir {current_local_full_path}: {e}")
                continue
        else:
            skipped_dirs += 1

        soup = BeautifulSoup(html_content, 'html.parser')
        menu_tag = soup.find('menu')
        if not menu_tag:
            print(f"WARNING: Could not find <menu> tag in {current_dir_view_url}")
            continue

        found_nodes_in_current_dir = False
        for img_tag in menu_tag.find_all('img', alt=True):
            alt_text = img_tag.get('alt', '').upper()
            
            parent_a_of_img = img_tag.parent
            if not parent_a_of_img or parent_a_of_img.name != 'a': 
                continue

            name_link_tag = parent_a_of_img.find_next_sibling('a')
            if not name_link_tag:
                if img_tag.next_sibling \
                and img_tag.next_sibling.next_sibling \
                and img_tag.next_sibling.next_sibling.name == 'a':
                    name_link_tag = img_tag.next_sibling.next_sibling
                else:
                    continue
            
            if not (name_link_tag and name_link_tag.name == 'a' and name_link_tag.get('href', '').startswith('./')): 
                continue

            href_value = name_link_tag['href'] 
            node_name_from_href = href_value[2:] 
            
            # Derive the intended local name primarily from node_name_from_href,
            # then sanitize just for illegal characters.
            if node_name_from_href.endswith('/'):
                temp_local_name = node_name_from_href[:-1] # e.g., "ivdowngrade"
            else:
                temp_local_name = node_name_from_href      # e.g., "GNUmakefile"
            
            displayed_node_name = sanitize_filename_for_illegal_chars(unquote(temp_local_name))
            if not displayed_node_name or displayed_node_name.lower() in ['parent directory', '[don\'t hide]', '[back]', 'attic', 'attic/']:
                continue
            
            print(f"\nFound node: Text='{displayed_node_name}', Href Suffix='{node_name_from_href}', Type='{alt_text}'")
            found_nodes_in_current_dir = True            
            next_item_view_url = urljoin(current_dir_view_url, node_name_from_href)
            next_item_full_repo_path = urljoin(current_full_repo_path, node_name_from_href)

            if alt_text == '[DIR]':
                print(f"DIR {saved_dirs}: {displayed_node_name}")
                next_local_rel_path = os.path.join(current_local_rel_path, displayed_node_name)
                next_local_rel_path = next_local_rel_path.replace("\\", "/")
                if not next_item_full_repo_path.endswith('/'):
                    next_item_full_repo_path += '/'
                queue.append((next_item_view_url, next_item_full_repo_path, next_local_rel_path))
                print(f"Added to queue: URL='{next_item_view_url}', FullRepoPath='{next_item_full_repo_path}', LocalSubPath='{next_local_rel_path}'")
            
            elif alt_text == '[TXT]':
                print(f"FILE {saved_files}: {displayed_node_name}")
                local_file_path = os.path.join(current_local_full_path, displayed_node_name)
                if os.path.exists(local_file_path) and os.path.getsize(local_file_path) > 0:
                    print(f"OK: Skipping: {local_file_path}")
                    skipped_files += 1
                    continue

                latest_revision = None
                rev_link_tag = name_link_tag.find_next_sibling('a')
                if rev_link_tag and rev_link_tag.find('b'):
                    latest_revision = rev_link_tag.find('b').get_text(strip=True)
                    print(f"Latest revision: {latest_revision}")

                if latest_revision:
                    path_for_download_url = next_item_full_repo_path.strip('/')
                    file_download_url = file_content = None
                    for fetch_file_content in [fetch_file_content_markup, fetch_file_content_checkout]:
                        file_download_url, file_content = fetch_file_content(cgi_base_url, latest_revision, path_for_download_url)
                        if file_content:
                            break

                    if file_content:
                        try:
                            with open(local_file_path, 'wb') as f:
                                f.write(file_content)
                            print(f"OK: Saved {local_file_path} (Rev: {latest_revision})")
                            saved_files += 1
                        except OSError as e:
                            print(f"ERROR saving {local_file_path}: {e}")
                            file_errors.append(('save error',file_download_url))
                    else:
                        print(f"ERROR: Failed to download rev {latest_revision} from: {file_download_url}")
                        file_errors.append(('download error',file_download_url))
                else:
                    print(f"ERROR: Could not find latest revision for {displayed_node_name} on directory page. Skipping.")
                    file_errors.append(('latest revision error',displayed_node_name))

        if not found_nodes_in_current_dir and menu_tag.find_all(['a', 'img']):
            print(f"WARNING: No parseable file/directory entries found in <menu> at {current_dir_view_url}")

    return saved_dirs, saved_files, skipped_dirs, skipped_files, file_errors

# --- Main execution ---
if __name__ == "__main__":
    saved_dirs, saved_files, skipped_dirs, skipped_files, file_errors = \
        fetch_latest_snapshot(FULL_URL)
    
    print(f"\nSummary:")
    print(f"Saved {saved_dirs} dirs and {saved_files} files.")
    print(f"Skipped {skipped_dirs} dirs and {skipped_files} files.")
    if file_errors:
        print("File errors:",len(file_errors))
        for error_type, error_value in file_errors:
            print(f"  {error_type}: {error_value}")
    else:
        print("No file errors.")
    print("\nDone!")