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
import subprocess
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/youtube.upload',
          'https://www.googleapis.com/auth/youtube.force-ssl']
REPO = json.loads((Path(__file__).resolve().parents[2] / 'daily' / 'config.json').read_text(encoding='utf-8'))['clips_repo']
GH = r'C:\Program Files\GitHub CLI\gh.exe'


DEFAULT_SECRET = Path(__file__).resolve().parents[2] / 'secrets' / 'client_secret.json'


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
    # access_type=offline + prompt=consent guarantees Google returns a refresh token.
    creds = flow.run_local_server(port=0, access_type='offline', prompt='consent',
                                  authorization_prompt_message='Opening your browser to log in to YouTube...')
    if not creds.refresh_token:
        sys.exit('Google did not return a refresh token. Remove the app at https://myaccount.google.com/permissions and run this again.')

    yt = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    chans = yt.channels().list(part='snippet', mine=True).execute().get('items', [])
    if not chans:
        sys.exit('Logged in, but this account has no YouTube channel. Run again and pick the YourChannel channel.')
    title = chans[0]['snippet']['title']
    print(f'\nAccess granted for YouTube channel: {title}')

    answer = input(f'Save YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN as GitHub secrets on {REPO}? [Y/n] ')
    if answer.strip().lower() not in ('', 'y', 'yes'):
        print('Not saved. Nothing was written anywhere.')
        return
    for name, value in (('YT_CLIENT_ID', app['client_id']), ('YT_CLIENT_SECRET', app['client_secret']),
                        ('YT_REFRESH_TOKEN', creds.refresh_token)):
        subprocess.run([GH, 'secret', 'set', name, '--repo', REPO], input=value, text=True, check=True)
        print(f'  saved {name}')
    print('\nDone. Tell Claude to run the check.')


if __name__ == '__main__':
    main()
