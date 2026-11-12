"""Build a clean SRT for one clip from cues.json (see vtt_to_cues.py).

Timestamps are shifted so the clip starts at 00:00:00.

Usage: python tools/make_clip_srt.py <cues.json> <start_sec> <end_sec> <out.srt>
"""
import json, sys, re
cues = json.load(open(sys.argv[1], encoding='utf-8'))
start, end, out = float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
def f(t):
    t=max(t,0); ms=int(round(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
sel=[c for c in cues if c[0]>=start-0.3 and c[0]<end]
n=0; lines=[]
for i,(s,e,t) in enumerate(sel):
    t = t.replace('\xa0',' ').replace('>>','').strip()
    t = re.sub(r'\[\s*__\s*\]', '[ __ ]', t)
    t = re.sub(r'\s+',' ',t)
    if not t: continue
    e = min(e, sel[i+1][0]) if i+1 < len(sel) else e
    s, e = max(s,start)-start, min(e,end)-start
    if e-s < 0.3: e = s+0.3
    n+=1; lines.append(f"{n}\n{f(s)} --> {f(e)}\n{t}\n")
open(out,'w',encoding='utf-8').write('\n'.join(lines))
print(out, n)
