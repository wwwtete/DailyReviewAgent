"""Bounded historical fact retrieval. Old reports are never factual evidence."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .core import Policy, stamp


def current_policy(config, policy):
    rules = {k: list(v) for k, v in policy.rules.items()}
    disabled = [k for k,v in config.get('sources', {}).items() if not v.get('enabled', True)]
    rules['sources'] = rules.get('sources', []) + disabled
    return Policy(rules)


def events_on(store, config, policy, day):
    policy = current_policy(config, policy)
    events = store.events(day, config['timezone'], policy)
    if config.get('sources', {}).get('codex', {}).get('mode') == 'archive':
        from .archive import init, events as archived
        init(store)
        events = [e for e in events if e['source'] != 'codex']
        if policy.allows({'source': 'codex'}):
            events += archived(store, day, config['timezone'], policy)
    return sorted(events, key=lambda e: (e['at'], e['id']))


def recent(store, config, policy, through, days=7):
    end = datetime.fromisoformat(through).date()
    return [e for offset in range(days-1, -1, -1)
            for e in events_on(store, config, policy, (end-timedelta(days=offset)).isoformat())]


def keys(event):
    # Project identity supplies context, NOT proof of an independent repeated task.
    result = set()
    for field in ('project_id', 'scope_id', 'remote', 'url', 'task_id'):
        if event.get(field): result.add((field, str(event[field])))
    if event.get('path'): result.add(('path', str(Path(event['path']).expanduser().resolve())))
    return result


def related(store, config, policy, through, seeds, days=30):
    identity = set().union(*(keys(e) for e in seeds)) if seeds else set()
    return [e for e in recent(store, config, policy, through, days) if keys(e) & identity]


def lookup(store, config, policy, ident):
    policy = current_policy(config, policy)
    if ident.startswith('codex:archive:'):
        if not policy.allows({'source': 'codex'}): raise ValueError('Source disabled')
        from .archive import evidence
        item = evidence(store, ident, policy)
        row = store.db.execute('SELECT day FROM codex_jobs WHERE id=?', (ident.split(':')[2],)).fetchone()
        # This also validates the fact index, metadata and current policy.
        for event in events_on(store, config, policy, row[0]):
            if event['id'] == ident: return event
    else:
        row = store.db.execute('SELECT data FROM events WHERE id=?', (ident,)).fetchone()
        if row:
            event = json.loads(row[0])
            if policy.allows(event): return event
    raise ValueError('Evidence unavailable under current policy')


def compact(event, timezone='Asia/Shanghai'):
    row = {k: event[k] for k in ('id', 'source', 'at', 'title', 'path', 'scope_id', 'project_id', 'url',
                                'author', 'task_id', 'thread_id', 'root_id', 'parent_id') if event.get(k)}
    row['day'] = stamp(event['at']).astimezone(ZoneInfo(timezone)).date().isoformat()
    if event.get('archive'):
        data = json.loads(event['body'])
        row.update(fact=data['fact']['text'], status=data['fact']['status'],
                   evidence_roles=data.get('evidence_roles', []), user_evidence=data.get('user_evidence', []))
    else:
        row['fact'] = event['body']
        row['excerpt'] = False
    return row


def read_evidence(store, config, policy, ident, cursor=None, limit=6000):
    """Content-bound cursor, contiguous text, current policy on every page."""
    from .source_facts import digest
    event=lookup(store,config,policy,ident)
    signature=digest(event)
    if cursor is None: start=0
    else:
        if not isinstance(cursor,dict) or cursor.get('sha256')!=signature or cursor.get('event_id')!=ident:
            raise ValueError('Evidence changed or cursor invalid')
        start=cursor.get('offset')
        if type(start) is not int or not 0<=start<len(event['body']): raise ValueError('Invalid evidence offset')
    if type(limit) is not int or not 256<=limit<=12000: raise ValueError('Evidence page limit must be 256..12000')
    end=min(len(event['body']),start+limit)
    return {**{k:v for k,v in event.items() if k!='body'},'text':event['body'][start:end],
            'start':start,'end':end,'total_chars':len(event['body']),'sha256':signature,
            'next_cursor':{'event_id':ident,'sha256':signature,'offset':end} if end<len(event['body']) else None}
