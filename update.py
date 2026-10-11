#!/usr/bin/env python3
"""NHL cards live-stats updater.  Runs in GitHub Actions every few hours.

  cards.json   the 192 current-deck cards (key "slug/Name", pos, born, bp)  -- written by Claude from the team files
  ids.json     card key -> NHL playerId, resolved once and cached here (edit by hand to fix a bad match)
  stats.json   what the cards read: this season, career, last 5 games, last season's PPG
  report.json  anything that could not be matched, or matched with a warning

Injuries: ESPN's public NHL injury feed. A card gets "inj" when ESPN lists the player on Injured Reserve
(IR / LTIR, whatever the reason -- not plain "Out", day-to-day or suspended) and he has not played a game since it was reported.

Only the Python standard library is used. Matching: NHL player search by surname -> active players ->
initial / position / birthplace filters -> CONFIRMED by birth date on the player's landing page.
"""
import json, os, re, sys, time, unicodedata, urllib.request, urllib.parse, datetime

UA = {'User-Agent': 'nhl-cards-live/1.0 (personal card project)'}
def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            if k == tries - 1: raise
            time.sleep(2 + 3 * k)

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c)).lower().strip()

MON = {m: i for i, m in enumerate(['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], 1)}
def iso(born):                       # "Jan 19, 2004" -> "2004-01-19"
    m = re.match(r'([A-Za-z]{3})\w*\s+(\d+),\s*(\d{4})', born or '')
    return '%s-%02d-%02d' % (m.group(3), MON[m.group(1).lower()], int(m.group(2))) if m else None

def season_now(today=None):
    d = today or datetime.date.today()
    y = d.year if d.month >= 9 else d.year - 1
    return y * 10000 + y + 1                       # 20262027

def landing(pid):
    return get('https://api-web.nhle.com/v1/player/%d/landing' % int(pid))

def resolve(card):
    name = card['name']; m = re.match(r'^([A-Z])\.\s*(.+)$', name)
    initial, surname = (m.group(1), m.group(2)) if m else (None, name)
    q = urllib.parse.quote(surname)
    res = get('https://search.d3.nhle.com/api/v1/search/player?culture=en-us&limit=100&q=' + q)
    cands = [r for r in res if r.get('active')]
    cands = [r for r in cands if norm(r['name']).split()[-1] == norm(surname).split()[-1] or norm(surname) in norm(r['name'])]
    if initial: cands = [r for r in cands if norm(r['name']).startswith(initial.lower())]
    goalie = card['pos'] == 'G'
    cands = [r for r in cands if (r.get('positionCode') == 'G') == goalie]
    want = iso(card['born']); city = norm((card.get('bp') or '').split(',')[0])
    if len(cands) > 1 and city:
        c2 = [r for r in cands if norm(r.get('birthCity')) == city]
        if c2: cands = c2
    # confirm by birth date (the only reliable key -- see context §13 "identity before figures")
    for r in cands:
        L = landing(r['playerId']); time.sleep(0.2)
        if L.get('birthDate') == want: return int(r['playerId']), None
    if len(cands) == 1:
        return int(cands[0]['playerId']), 'birth date did not match (card %s) -- accepted as the only active %s' % (card['born'], surname)
    return None, '%d candidates, none with birth date %s' % (len(cands), card['born'])


# ---------- injuries (ESPN public feed) ----------
ESPN_TEAMS = {'anaheim':'ANA','boston':'BOS','buffalo':'BUF','calgary':'CGY','carolina':'CAR','chicago':'CHI','colorado':'COL',
  'columbus':'CBJ','dallas':'DAL','detroit':'DET','edmonton':'EDM','florida':'FLA','los angeles':'LAK','minnesota':'MIN',
  'montreal':'MTL','nashville':'NSH','new jersey':'NJD','islanders':'NYI','rangers':'NYR','ottawa':'OTT','philadelphia':'PHI',
  'pittsburgh':'PIT','san jose':'SJS','seattle':'SEA','st. louis':'STL','st louis':'STL','tampa':'TBL','toronto':'TOR','utah':'UTA',
  'vancouver':'VAN','vegas':'VGK','washington':'WSH','winnipeg':'WPG'}
def espn_abbrev(name):
    n = norm(name)
    for k, v in ESPN_TEAMS.items():
        if k in n: return v
    return None

def injuries(report):
    """-> {(TEAM, 'first last'): {...}} for players ESPN lists on Injured Reserve (IR / LTIR)."""
    try:
        d = get('https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries')
    except Exception as ex:
        report['errors']['_injuries'] = 'ESPN feed: %s' % ex; return None
    out = {}
    for t in d.get('injuries') or []:
        ab = espn_abbrev(t.get('displayName') or '')
        for i in t.get('injuries') or []:
            st = (i.get('status') or '').strip(); typ = ((i.get('type') or {}).get('name') or '').upper()
            sl = st.lower()
            if 'day' in sl or 'susp' in sl or 'SUSPENSION' in typ or 'DAY' in typ: continue
            if not ('reserve' in sl or 'RESERVE' in typ or re.search(r'\bl?ti?r\b', sl)): continue   # IR / LTIR only
            a = i.get('athlete') or {}; nm = norm(a.get('displayName') or '')
            if not nm: continue
            det = i.get('details') or {}
            what = ' '.join(x for x in [det.get('side') if det.get('side') not in (None, 'Not Specified') else None,
                                         det.get('type'), det.get('detail')] if x) or None
            r = (det.get('returnDate') or '')[:10]
            out[(ab, nm)] = {'s': st or 'Out', 'd': what, 'r': r or None, 'since': (i.get('date') or '')[:10] or None}
    return out

def match_injury(inj, L, team):
    if not inj: return None
    first = norm((L.get('firstName') or {}).get('default')); last = norm((L.get('lastName') or {}).get('default'))
    hit = inj.get((team, first + ' ' + last))
    if not hit:          # Alex/Alexander, Matt/Matthew ...: same team, same surname, same first initial, and only one
        c = [v for (ab, nm), v in inj.items() if ab == team and nm.split()[-1] == last.split()[-1] and nm[:1] == first[:1]]
        hit = c[0] if len(c) == 1 else None
    if not hit: return None
    last_game = max([g.get('gameDate', '') for g in (L.get('last5Games') or [])] or [''])
    if hit.get('since') and last_game and last_game > hit['since']: return None   # has played since -> back
    return {k: v for k, v in hit.items() if k != 'since' and v}

def pct3(x): return ('%.3f' % x).lstrip('0') if x is not None else None

def build(pid, goalie, cur, inj=None):
    L = landing(pid)
    fs = L.get('featuredStats') or {}; rs = fs.get('regularSeason') or {}
    sub = rs.get('subSeason') if int(fs.get('season') or 0) == cur else None
    car = rs.get('career') or {}
    pid = int(pid)
    e = {'id': pid, 'num': L.get('sweaterNumber'), 'team': L.get('currentTeamAbbrev')}
    if goalie:
        e['g'] = 1
        e['cur'] = [sub['gamesPlayed'], sub.get('wins', 0), sub.get('losses', 0), sub.get('savePctg'), sub.get('goalsAgainstAvg')] if sub and sub.get('gamesPlayed') else None
        e['car'] = [car.get('gamesPlayed', 0), car.get('wins', 0), car.get('losses', 0),
                    pct3(car.get('savePctg')), '%.2f' % car['goalsAgainstAvg'] if car.get('goalsAgainstAvg') is not None else None]
        e['l5'] = [[g['gameDate'][5:], g.get('opponentAbbrev', ''), g.get('decision', ''), g.get('shotsAgainst', 0), g.get('goalsAgainst', 0)]
                   for g in (L.get('last5Games') or []) if g.get('gameTypeId', 2) == 2 and str(g.get('gameDate', '')) >= str(cur // 10000) + '-09-01']
    else:
        e['g'] = 0
        e['cur'] = [sub['gamesPlayed'], sub.get('goals', 0), sub.get('assists', 0), sub.get('points', 0)] if sub and sub.get('gamesPlayed') else None
        e['car'] = [car.get('gamesPlayed', 0), car.get('goals', 0), car.get('assists', 0), car.get('points', 0)]
        e['l5'] = [[g['gameDate'][5:], g.get('opponentAbbrev', ''), g.get('goals', 0), g.get('assists', 0), g.get('points', 0)]
                   for g in (L.get('last5Games') or []) if g.get('gameTypeId', 2) == 2 and str(g.get('gameDate', '')) >= str(cur // 10000) + '-09-01']
        prev = cur - 10001                               # 20252026
        gp = pts = 0
        for s in L.get('seasonTotals') or []:
            if s.get('season') == prev and s.get('leagueAbbrev') == 'NHL' and s.get('gameTypeId') == 2:
                gp += s.get('gamesPlayed', 0); pts += s.get('points', 0)
        e['base'] = round(pts / gp, 3) if gp >= 20 else None     # "usual pace" for the cold rule
    j = match_injury(inj, L, e['team'])
    if j: e['inj'] = j
    return e

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    P = lambda f: os.path.join(here, f)
    cards = json.load(open(P('cards.json'), encoding='utf-8'))
    ids = json.load(open(P('ids.json'), encoding='utf-8')) if os.path.exists(P('ids.json')) else {}
    report = {'unmatched': {}, 'warnings': {}, 'errors': {}}
    for c in cards:
        if c['key'] in ids: continue
        try:
            pid, warn = resolve(c)
            if pid: ids[c['key']] = pid
            if warn: (report['warnings'] if pid else report['unmatched'])[c['key']] = warn
        except Exception as ex:
            report['errors'][c['key']] = 'resolve: %s' % ex
        time.sleep(0.2)
    json.dump(ids, open(P('ids.json'), 'w', encoding='utf-8'), indent=1, sort_keys=True)
    cur = season_now(); label = '%02d-%02d' % (cur // 10000 % 100, cur % 100)
    players = {}
    inj = injuries(report)
    for c in cards:
        pid = ids.get(c['key'])
        if not pid: continue
        try:
            players[c['key']] = build(pid, c['pos'] == 'G', cur, inj)
        except Exception as ex:
            report['errors'][c['key']] = 'stats: %s' % ex
        time.sleep(0.25)
    out = {'season': label, 'updated': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'), 'players': players}
    json.dump(out, open(P('stats.json'), 'w', encoding='utf-8'), separators=(',', ':'), ensure_ascii=False)
    report['matched'] = len(players); report['cards'] = len(cards)
    report['injured'] = sorted(k for k, v in players.items() if v.get('inj'))
    report['espn_ir_listed'] = len(inj) if inj is not None else None
    json.dump(report, open(P('report.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    print('matched %d/%d, unmatched %d, warnings %d, errors %d' % (len(players), len(cards), len(report['unmatched']), len(report['warnings']), len(report['errors'])))

if __name__ == '__main__':
    main()
