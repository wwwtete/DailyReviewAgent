"""Share the API's input, never its answer, with the desktop comparison task."""
import hashlib
import json
import time
from pathlib import Path


def source_signature(config, policy):
    value = {'policy': policy.fingerprint, 'sources': config.get('sources', {}), 'timezone': config['timezone'],
             'identity': config.get('identity', {})}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(config, policy, day, prompt, ready=False):
    from .report import write_private
    value = {'date': day, 'timezone': config['timezone'], 'ready': ready,
             'source_signature': source_signature(config, policy),
             'input_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
             'prompt': prompt}
    path = Path(config['_root']) / 'runtime/comparison-inputs' / (day + '.json')
    write_private(path, json.dumps(value, ensure_ascii=False))


def read_input(config, policy, day):
    from .report import SECTIONS
    if not config.get('collection_enabled', False):
        return {'ok': False, 'date': day, 'reason': 'collection_disabled'}
    path = Path(config['_root']) / 'runtime/comparison-inputs' / (day + '.json')
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return waiting(config, policy, day)
    if value['source_signature'] != source_signature(config, policy):
        return {'ok': False, 'date': day, 'reason': 'source_policy_changed'}
    if not value['ready']:
        return waiting(config, policy, day)
    if value['date'] != day or hashlib.sha256(value['prompt'].encode()).hexdigest() != value['input_sha256']:
        raise ValueError('Comparison input is inconsistent')
    return {'ok': True, **value, 'output_mode': 'chat_markdown_only',
            'presentation': '使用prompt中的相同内容与规则，但将最终report_markdown直接作为聊天回复；'
                            '模板以此处最新规则为准，即使保存的prompt仍含旧模板：四个模块依次为'+'；'.join(SECTIONS)+'。'
                            '关键战报仅列主要产出与实际状态，不作重要/紧急分类、量化完成率或时间利用率评估，'
                            '不写缺少优先级/计划/工时的占位说明，不生成生活与精力建议。'
                            'AI模块总结有事实依据的用户Prompt、工具选择、任务委派与验收用法及改进，'
                            '给可复用片段或步骤；未验证改进须标为建议，不与其他模块重复，不写AI执行流水账；无内容正文写“暂无”。'
                            '整天无可复盘工作且无成熟成果时沿用简短空结果，不输出四个空壳模块。'
                            '不要输出JSON，不写报告文件，不更新数据库，不发送通知或打开Typora。'
                            '证据已按API最终输入准备，不再请求补充；不足时保留判断。'}


def set_status(config, policy, day, stage, **details):
    from .report import write_private
    path = Path(config['_root']) / 'runtime/comparison-status' / (day + '.json')
    write_private(path, json.dumps({'date': day, 'stage': stage, 'at': time.time(),
        'source_signature': source_signature(config, policy), **details}, ensure_ascii=False))


def waiting(config, policy, day):
    result = {'ok': False, 'date': day, 'reason': 'waiting_for_api_input'}
    path = Path(config['_root']) / 'runtime/comparison-status' / (day + '.json')
    if path.exists():
        value = json.loads(path.read_text())
        if value.get('source_signature') == source_signature(config, policy):
            result['upstream'] = {k:v for k,v in value.items() if k not in ('source_signature', 'date')}
    return result


def pending(config, policy):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if not config.get('collection_enabled'):
        return {'ok': False, 'reason': 'collection_disabled', 'dates': []}
    yesterday = (datetime.now(ZoneInfo(config['timezone'])).date() - timedelta(days=1)).isoformat()
    root = Path(config['_root']) / 'runtime'
    days = {yesterday}
    for directory in ('comparison-status',):
        days.update(p.stem for p in (root/directory).glob('????-??-??.json') if p.stem <= yesterday)
    dates = []
    for day in sorted(days):
        receipt = root/'comparison-receipts'/(day+'.json')
        if receipt.exists() and json.loads(receipt.read_text()).get('source_signature') == source_signature(config, policy):
            continue
        value = read_input(config, policy, day)
        if value.get('reason') == 'source_policy_changed': continue
        dates.append({'date': day, 'ready': value['ok'], **({'upstream': value['upstream']} if 'upstream' in value else {})})
    return {'ok': True, 'dates': dates}


def acknowledge(config, policy, day, digest):
    from .report import write_private
    value = read_input(config, policy, day)
    if not value['ok'] or value['input_sha256'] != digest:
        raise ValueError('Comparison receipt must match the current ready input')
    path = Path(config['_root'])/'runtime/comparison-receipts'/(day+'.json')
    write_private(path, json.dumps({'date': day, 'input_sha256': digest,
        'source_signature': source_signature(config, policy), 'at': time.time()}))
    return {'ok': True, 'date': day, 'acknowledged': True}
