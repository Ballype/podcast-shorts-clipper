"""One-time YouTube login for the cloud poster (no Composio).

Run it yourself on this PC (reads secrets/client_secret.json, which git ignores):
    python scripts/post/youtube_auth.py

1. A browser opens. Log in with the Google account that owns YourChannel and PICK THE YOUR CHANNEL CHANNEL
   if Google asks which channel. Approve every permission. ("Google hasn't verified this app" is
   expected for your own app: click Advanced -> Go to ... (unsafe).)
2. The script shows which channel it got access to, then offers to save three GitHub secrets
   (YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN) with the gh CLI. Nothing is printed or stored
   anywhere else.

The client_secret JSON comes from Google Cloud Console -> APIs & Services -> Credentials ->
your OAuth client ID (type "Desktop app") -> Download JSON.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import json
import os
import subprocess
import sys
import time
import urllib.parse
import wsgiref.simple_server
import wsgiref.util
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow, _ExclusiveWSGIServer, _WSGIRequestHandler
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/youtube.upload',
          'https://www.googleapis.com/auth/youtube.force-ssl']
REPO = json.loads((Path(__file__).resolve().parents[2] / 'daily' / 'config.json').read_text(encoding='utf-8'))['clips_repo']
GH = r'C:\Program Files\GitHub CLI\gh.exe'


DEFAULT_SECRET = Path(__file__).resolve().parents[2] / 'secrets' / 'client_secret.json'


class OAuthRedirectApp:
    def __init__(self):
        self.auth_response = None

    def __call__(self, environ, start_response):
        uri = wsgiref.util.request_uri(environ)
        if 'code=' in uri or 'error=' in uri:
            self.auth_response = uri
            start_response("200 OK", [("Content-type", "text/html; charset=utf-8")])
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Authorization Successful</title></head>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #0f172a; color: #f8fafc;">
                <div style="text-align: center; background: #1e293b; padding: 40px 60px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); max-width: 500px;">
                    <div style="font-size: 54px; margin-bottom: 16px;">✅</div>
                    <h1 style="margin: 0 0 12px; font-size: 24px; color: #38bdf8;">Authorization Successful!</h1>
                    <p style="color: #94a3b8; font-size: 16px; margin: 0 0 24px;">Your YouTube credentials have been captured. You can safely close this window and return to the assistant.</p>
                </div>
            </body>
            </html>
            """
            return [html.encode("utf-8")]
        start_response("404 Not Found", [("Content-type", "text/plain")])
        return [b"Not found"]


def run_local_server_with_url(flow, port=8090):
    wsgi_app = OAuthRedirectApp()
    local_server = None
    try:
        local_server = wsgiref.simple_server.make_server(
            '127.0.0.1', port, wsgi_app,
            server_class=_ExclusiveWSGIServer,
            handler_class=_WSGIRequestHandler
        )
    except Exception:
        # Fallback to ephemeral port if 8090 is taken
        local_server = wsgiref.simple_server.make_server(
            '127.0.0.1', 0, wsgi_app,
            server_class=_ExclusiveWSGIServer,
            handler_class=_WSGIRequestHandler
        )

    try:
        actual_port = local_server.server_port
        flow.redirect_uri = f'http://localhost:{actual_port}/'
        auth_url, _ = flow.authorization_url(access_type='offline', prompt='consent')
        
        # Save to secrets/auth_url.txt
        url_file = Path(__file__).resolve().parents[2] / 'secrets' / 'auth_url.txt'
        url_file.write_text(auth_url, encoding='utf-8')
        print(f'\nAUTHORIZATION_URL: {auth_url}\n', flush=True)

        try:
            os.startfile(auth_url)
        except Exception:
            pass

        local_server.timeout = 2
        start_time = time.time()
        while not wsgi_app.auth_response:
            local_server.handle_request()
            if time.time() - start_time > 1200:  # 20 min timeout
                raise TimeoutError("Authorization timed out after 20 minutes.")

        auth_response = wsgi_app.auth_response.replace('http:', 'https:')
        flow.fetch_token(authorization_response=auth_response)
    finally:
        if local_server:
            local_server.server_close()

    return flow.credentials


def main():
    secret_file = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_SECRET)
    if not Path(secret_file).exists():
        sys.exit(f'Put the JSON you downloaded from Google Cloud at {DEFAULT_SECRET} (folder is git-ignored), then run again.')
    cfg = json.loads(Path(secret_file).read_text(encoding='utf-8'))
    app = cfg.get('installed') or cfg.get('web') or {}
    if 'installed' not in cfg:
        print('Note: this client is not a "Desktop app" type. If login fails with a redirect error, '
              'create a Desktop app OAuth client instead.')

    flow = InstalledAppFlow.from_client_secrets_file(secret_file, SCOPES)
    creds = run_local_server_with_url(flow, port=0)
    if not creds.refresh_token:
        sys.exit('Google did not return a refresh token. Remove the app at https://myaccount.google.com/permissions and run this again.')

    yt = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    chans = yt.channels().list(part='snippet', mine=True).execute().get('items', [])
    if not chans:
        sys.exit('Logged in, but this account has no YouTube channel. Run again and pick the YourChannel channel.')
    title = chans[0]['snippet']['title']
    print(f'\nAccess granted for YouTube channel: {title}')

    # Save tokens locally to secrets/tokens.json (git-ignored)
    tokens_file = Path(__file__).resolve().parents[2] / 'secrets' / 'tokens.json'
    tokens_data = {
        'client_id': app['client_id'],
        'client_secret': app['client_secret'],
        'refresh_token': creds.refresh_token,
        'channel_title': title
    }
    tokens_file.write_text(json.dumps(tokens_data, indent=2), encoding='utf-8')
    print(f'Saved local credentials to {tokens_file}')

    # Also update .env with these variables if .env exists
    env_file = Path(__file__).resolve().parents[2] / '.env'
    if env_file.exists():
        env_lines = env_file.read_text(encoding='utf-8').splitlines()
        keys_to_set = {
            'YT_CLIENT_ID': app['client_id'],
            'YT_CLIENT_SECRET': app['client_secret'],
            'YT_REFRESH_TOKEN': creds.refresh_token
        }
        new_lines = []
        existing_keys = set()
        for line in env_lines:
            matched = False
            for k, v in keys_to_set.items():
                if line.startswith(f'{k}='):
                    new_lines.append(f'{k}={v}')
                    existing_keys.add(k)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        for k, v in keys_to_set.items():
            if k not in existing_keys:
                new_lines.append(f'{k}={v}')
        env_file.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
        print(f'Updated {env_file} with YouTube credentials')

    if REPO and 'YOUR_GITHUB_USER' not in REPO:
        answer = input(f'Save YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN as GitHub secrets on {REPO}? [Y/n] ')
        if answer.strip().lower() in ('', 'y', 'yes'):
            for name, value in (('YT_CLIENT_ID', app['client_id']), ('YT_CLIENT_SECRET', app['client_secret']),
                                ('YT_REFRESH_TOKEN', creds.refresh_token)):
                subprocess.run([GH, 'secret', 'set', name, '--repo', REPO], input=value, text=True, check=True)
                print(f'  saved {name} to GitHub')
    else:
        print(f'Note: GitHub repo in daily/config.json is still default ({REPO}). Tokens saved locally.')
    print('\nDone! Authentication successful.')


if __name__ == '__main__':
    main()
