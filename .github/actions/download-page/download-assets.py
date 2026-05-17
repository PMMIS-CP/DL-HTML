import os, sys, re, json, hashlib, subprocess, urllib.parse, time
from html.parser import HTMLParser

# Environment
page_dir = os.environ['PAGE_DIR']
html_file = os.environ['HTML_FILE']
input_url = os.environ.get('INPUT_URL', '')
user_agent = os.environ['INPUT_USER_AGENT']
timeout = os.environ['INPUT_DOWNLOAD_TIMEOUT']
preserve_protocol = os.environ.get('INPUT_PRESERVE_PROTOCOL', 'false').lower() == 'true'
css_dir = os.path.join(page_dir, 'assets', 'css')
js_dir = os.path.join(page_dir, 'assets', 'js')
logs_dir = os.path.join(page_dir, 'logs')

# Read HTML
with open(html_file, 'r', encoding='utf-8', errors='replace') as f:
    html_content = f.read()

# Determine base URL from <base> tag or input URL
base_url = input_url
base_match = re.search(r'<base\s[^>]*href=["\']?([^"\'\s>]+)', html_content, re.IGNORECASE)
if base_match:
    base_href = base_match.group(1)
    base_url = urllib.parse.urljoin(input_url, base_href)

# Extract the scheme of the base URL for preserve-protocol logic
base_scheme = ''
if base_url:
    parsed_base = urllib.parse.urlparse(base_url)
    base_scheme = parsed_base.scheme

# Parse HTML and collect CSS/JS links
class AssetExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.css_links = set()
        self.js_links = set()
    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): v for k, v in attrs}
        href = attrs.get('href', '').strip()
        src = attrs.get('src', '').strip()

        def is_safe(url):
            return not url.startswith('data:') and not url.startswith('blob:') and not url.startswith('javascript:')

        if tag == 'link' and href and is_safe(href):
            rel = attrs.get('rel', '').lower()
            if rel == 'stylesheet' or attrs.get('as', '').lower() == 'style':
                self.css_links.add(href)
        elif tag == 'script' and src and is_safe(src):
            self.js_links.add(src)

extractor = AssetExtractor()
extractor.feed(html_content)
css_urls = list(extractor.css_links)
js_urls = list(extractor.js_links)

print(f"Found {len(css_urls)} CSS files, {len(js_urls)} JS files")

# URL resolver with preserve-protocol support
def resolve_url(relative_url, base_url, preserve=False, base_scheme=''):
    if not base_url:
        if urllib.parse.urlparse(relative_url).scheme or relative_url.startswith('//'):
            return relative_url
        else:
            return None
    resolved = urllib.parse.urljoin(base_url, relative_url)
    if preserve and relative_url.startswith('//') and base_scheme:
        parsed = urllib.parse.urlparse(resolved)
        resolved = urllib.parse.urlunparse(parsed._replace(scheme=base_scheme))
    return resolved

download_log = []
os.makedirs(css_dir, exist_ok=True)
os.makedirs(js_dir, exist_ok=True)

def download_asset(url, dest_dir, asset_type, log_list):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ('data', 'blob', 'javascript'):
            log_list.append({"url": url, "file": "", "status": "skipped", "reason": "unsupported scheme"})
            print(f"  SKIP: {url} (unsupported scheme)")
            return False

        path = parsed.path
        filename = os.path.basename(path)
        if not filename or filename.startswith('.'):
            filename = f"{asset_type}_{hashlib.sha256(url.encode()).hexdigest()[:8]}"
        else:
            filename = re.sub(r'\?.*$', '', filename)
            filename = re.sub(r'[^\w\-.]', '_', filename)

        temp_dest = os.path.join(dest_dir, f".tmp_{filename}")
        cmd = [
            'curl', '-s', '-L',
            '--compressed',
            '--max-time', timeout,
            '--retry', '2',
            '--retry-delay', '1',
            '-A', user_agent,
            '-H', 'Accept: */*',
            '--fail',
            '-o', temp_dest,
            '--write-out', '%{content_type}',
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        content_type = result.stdout.strip().split(';')[0].strip().lower() if result.stdout else ''
        curl_exit = result.returncode

        if curl_exit == 0 and os.path.getsize(temp_dest) > 0:
            ext_map = {
                'text/css': '.css',
                'application/javascript': '.js',
                'application/x-javascript': '.js',
                'text/javascript': '.js'
            }
            proper_ext = ext_map.get(content_type, f'.{asset_type}')
            base, old_ext = os.path.splitext(filename)
            if old_ext.lower() not in ('.css', '.js'):
                final_filename = base + proper_ext
            else:
                final_filename = filename

            final_dest = os.path.join(dest_dir, final_filename)
            counter = 1
            while os.path.exists(final_dest):
                name, ext = os.path.splitext(final_filename)
                final_dest = os.path.join(dest_dir, f"{name}_{counter}{ext}")
                counter += 1

            os.rename(temp_dest, final_dest)
            log_list.append({"url": url, "file": os.path.basename(final_dest), "status": "success", "content_type": content_type})
            print(f"  OK: {os.path.basename(final_dest)} ({content_type})")
            return True
        else:
            if os.path.exists(temp_dest):
                os.remove(temp_dest)
            err_msg = result.stderr.strip() if result.stderr else f"curl exit code {curl_exit}"
            log_list.append({"url": url, "file": filename, "status": "failed", "error": err_msg})
            print(f"  FAIL: {url}  -> {err_msg}")
            return False
    except Exception as e:
        log_list.append({"url": url, "file": filename, "status": "error", "error": str(e)})
        print(f"  ERROR: {url} - {e}")
        return False

# Download CSS
for css in css_urls:
    full_url = resolve_url(css, base_url, preserve_protocol, base_scheme)
    if full_url:
        download_asset(full_url, css_dir, 'css', download_log)
    else:
        download_log.append({"url": css, "status": "skipped", "reason": "relative URL without base"})

# Download JS
for js in js_urls:
    full_url = resolve_url(js, base_url, preserve_protocol, base_scheme)
    if full_url:
        download_asset(full_url, js_dir, 'js', download_log)
    else:
        download_log.append({"url": js, "status": "skipped", "reason": "relative URL without base"})

# Save asset download log
with open(os.path.join(logs_dir, 'assets-download.json'), 'w') as f:
    json.dump(download_log, f, indent=2)

css_count = len([f for f in os.listdir(css_dir) if f.endswith('.css')]) if os.path.isdir(css_dir) else 0
js_count = len([f for f in os.listdir(js_dir) if f.endswith('.js')]) if os.path.isdir(js_dir) else 0
total_assets = css_count + js_count

with open(os.environ['GITHUB_OUTPUT'], 'a') as gh:
    gh.write(f"css_count={css_count}\n")
    gh.write(f"js_count={js_count}\n")
    gh.write(f"total_assets={total_assets}\n")

print(f"\nAsset download complete: CSS={css_count}, JS={js_count}, Total={total_assets}")