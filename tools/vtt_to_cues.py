"""Extract clean, non-duplicated timed cues from a YouTube auto-caption VTT.

YouTube rolling captions repeat each line 2-3 times; only the word-timed
(<c>-tagged) lines carry new text, so we keep just those.

Usage: python tools/vtt_to_cues.py <input.vtt> <cues.json>
"""
import re, sys, html, json
src = sys.argv[1]
txt = open(src, encoding='utf-8').read()
def ts(s):
    h,m,r = s.split(':'); return int(h)*3600+int(m)*60+float(r)
cues=[]
for b in re.split(r'\n\n+', txt):
    m = re.search(r'(\d\d:\d\d:\d\d\.\d+) --> (\d\d:\d\d:\d\d\.\d+)', b)
    if not m: continue
    for l in b.split('\n')[1:]:
        if '<c>' in l or re.search(r'<\d\d:', l):
            t = html.unescape(re.sub(r'<[^>]+>', '', l)).strip()
            if t: cues.append([ts(m.group(1)), ts(m.group(2)), t])
for i in range(len(cues)-1):
    cues[i][1] = max(cues[i][1], cues[i+1][0]) if cues[i+1][0]-cues[i][0] < 6 else cues[i][1]
json.dump(cues, open(sys.argv[2],'w',encoding='utf-8'))
print(len(cues))
