"""A fresh image challenge verifies basic vision and file-writing behavior."""
import hashlib
import json
import secrets
from pathlib import Path

from _common import atomic_text, inside, read_json, sha256, write_json

STATE = '_工作区/能力探针/current.json'


def read_state(course):
    return read_json(course / STATE) if (course / STATE).exists() else {}


def save_state(course, data):
    write_json(course / STATE, data)


STOP = '当前子代理未通过读图转写能力探针。停止转写与课程生成，请更换支持图片理解的模型并重新探测；不得靠目录、文件名或常识补写材料。'


def normalized(value):
    return ''.join(value.split()).lower()


def answer_hash(value):
    return hashlib.sha256(normalized(value).encode('utf-8')).hexdigest()


def start(course, host, model):
    from convert_materials import load_ledger, now, LEDGER
    from host_agents import HOSTS
    if host not in HOSTS or not model.strip():
        raise ValueError('必须指定当前宿主和实际子代理模型')
    data = read_state(course)
    ledger = load_ledger(course) if (course / LEDGER).exists() else {'sources': []}
    if any(b.get('active_member') for s in ledger['sources'] for b in s['batches']):
        raise ValueError('先确认旧成员停止并释放租约，再切换模型做探针')
    from PIL import Image, ImageDraw, ImageFont
    nonce = secrets.token_hex(8)
    folder = '_工作区/能力探针/' + nonce
    image_path = folder + '/challenge.png'
    result_path = folder + '/response.json'
    lines = [secrets.token_hex(4).upper(), secrets.token_hex(4).upper(),
             f"y = {secrets.randbelow(8)+2}x + {secrets.randbelow(80)+10}"]
    image = Image.new('RGB', (420, 140), 'white')
    ImageDraw.Draw(image).multiline_text((20, 20), '\n'.join(lines),
        font=ImageFont.load_default(), fill='black', spacing=16)
    path = inside(course, image_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize((1260, 420)).save(path)
    spec = HOSTS[host]
    prompt = (f"这是能力探针。只用 {spec['read_image']} 实际查看 {path}，从上到下转写图中三行字符。"
              f"用 {spec['write']} 将 JSON 写入 {inside(course, result_path)}。"
              '格式：{"can_read_images":true,"lines":["第一行","第二行","第三行"]}。'
              '如果不能看图，写 {"can_read_images":false,"lines":[]}，不要猜测。'
              '只能读给出的图片和复核自己的输出，禁止读其他文件、目录、脚本、哈希或历史答案；'
              '禁止 shell、OCR、联网和派生代理。只回 PROBE_SAVED 或 PROBE_FAILED，不返回图中内容。')
    prompt_path = folder + '/prompt.txt'
    atomic_text(inside(course, prompt_path), prompt)
    data['probe'] = {'version': 2, 'passed': False, 'host': host, 'model': model,
                     'at': now(), 'image': image_path, 'image_hash': sha256(path),
                     'response': result_path, 'expected': [answer_hash(v) for v in lines]}
    data['capability_blocked'] = True
    save_state(course, data)
    return {'image': image_path, 'response': result_path, 'prompt': prompt_path,
            'host': host, 'model': model, 'next': '用指定模型新建宿主成员执行提示，再运行 probe'}


def finish(course, member, host, model):
    from convert_materials import now
    data = read_state(course)
    probe = data.get('probe', {})
    reason = ''
    try:
        if (probe.get('version') != 2 or probe.get('host') != host or probe.get('model') != model):
            raise ValueError('缺少本宿主/模型的新视觉探针')
        if sha256(inside(course, probe['image'])) != probe['image_hash']:
            raise ValueError('探针图片发生变化')
        result = read_json(inside(course, probe['response']))
        lines = result.get('lines')
        if result.get('can_read_images') is not True:
            raise ValueError('成员报告无法读图')
        if not isinstance(lines, list) or len(lines) != 3 or not all(isinstance(v, str) for v in lines):
            raise ValueError('未返回完整图像转写')
        if [answer_hash(v) for v in lines] != probe['expected']:
            raise ValueError('图像转写与随机测试内容不一致')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc)
    probe.update(passed=not reason, member=member, checked_at=now(), failure=reason)
    if not reason:
        probe['response_hash'] = sha256(inside(course, probe['response']))
    data['probe'] = probe
    data['capability_blocked'] = bool(reason)
    save_state(course, data)
    if reason:
        raise ValueError(STOP + ' 原因：' + reason)
    return {'probe': 'passed', 'capabilities': ['image_transcription', 'write_file'],
            'host': host, 'model': model, 'member': member}


def require(data, course, host, model):
    data = read_state(course)
    probe = data.get('probe', {})
    if (data.get('capability_blocked') or probe.get('version') != 2 or not probe.get('passed')
            or probe.get('host') != host or probe.get('model') != model):
        raise ValueError(STOP + ' 先运行 probe-start / probe；旧写入探针不再有效。')
    if (sha256(inside(course, probe['response'])) != probe.get('response_hash')
            or sha256(inside(course, probe['image'])) != probe.get('image_hash')):
        raise ValueError(STOP + ' 探针证据已改变。')
