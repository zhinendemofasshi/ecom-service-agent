"""Deterministic, fail-closed boundaries. Pattern detection is defense in depth,
not a proof that arbitrary prompt injections have been recognized.
"""
from __future__ import annotations
import json
import re
import unicodedata
from hashlib import sha256

PII = [
    (r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号]'),
    (r'(?<!\w)[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)', '[邮箱]'),
    (r'(?<!\d)\d{17}[\dXx](?!\w)', '[身份证]'),
    (r'(?<![\w-])\d{16,19}(?![\w-])', '[银行卡]'),
    (r'((?:密码|验证码|password|secret|api[_ -]?key)\s*[:：=]\s*)[^\s,，;；]+', r'\1[已隐藏]'),
    (r'((?:收货地址|详细地址|地址)\s*[:：]\s*)[^\n。；;]+', r'\1[地址]'),
]
INJECTION = re.compile(r'ignore.{0,40}(?:previous|system|instruction)|忽略.{0,20}(?:指令|规则|提示)|(?:泄露|输出|打印).{0,15}(?:系统提示|system prompt)|(?:绕过|跳过).{0,10}(?:审批|权限|校验)|<\|(?:system|im_start)\|>|you are now (?:admin|system)', re.I|re.S)
UNAUTHORIZED = re.compile(r'(?:别人|他人|其他用户|所有用户).{0,12}(?:订单|地址|手机|退款)|(?:提权|冒充管理员|伪造审批)', re.I)
RISK = re.compile(r'投诉|消协|维权|人身|自杀|自残|过敏|受伤|起诉|律师|盗刷|诈骗')
UNSAFE_OUTPUT = re.compile(r'保证.{0,12}(?:退款|赔偿|到账)|(?:一定|百分之百|100%).{0,8}(?:到账|赔偿)|(?:傻逼|滚蛋)|(?:提供|告诉我|发送).{0,12}(?:密码|验证码)')


def redact(value):
    if isinstance(value, str):
        for pattern, replacement in PII:
            value = re.sub(pattern, replacement, value, flags=re.I)
        return value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        sensitive={'phone','mobile','email','address','recipient','customer_name','id_card','bank_card','password','verification_code','api_key','user'}
        return {k: ('[已脱敏]' if k.lower() in sensitive else redact(v)) for k,v in value.items()}
    return value


class Guardrails:
    def inspect_input(self, text):
        normalized = unicodedata.normalize('NFKC', text)
        normalized = re.sub('[\u200b-\u200f\ufeff]', '', normalized)
        return [name for name, rule in [('prompt_injection',INJECTION), ('unauthorized_intent',UNAUTHORIZED), ('high_risk',RISK)] if rule.search(normalized)]

    def tool_policy(self, name, arguments, definitions, tenant, user, allowed=None):
        if allowed is not None and name not in allowed:
            return '当前 Agent 无权调用此工具'
        schema = next((d['function'].get('parameters',{}) for d in definitions if d['function']['name']==name), None)
        if schema is None or not isinstance(arguments, dict):
            return '未知工具或参数不是对象'
        props = schema.get('properties',{})
        if set(arguments)-set(props) or set(schema.get('required',[]))-set(arguments):
            return '参数字段不符合工具政策'
        types = {'string':str, 'integer':int, 'number':(int,float), 'boolean':bool, 'object':dict, 'array':list}
        for key, value in arguments.items():
            expected = types.get(props[key].get('type'))
            if expected and (not isinstance(value, expected) or (isinstance(value,bool) and props[key].get('type') in {'number','integer'})):
                return '参数类型无效'
            if isinstance(value,str) and (len(value)>4000 or INJECTION.search(value)):
                return '参数包含不允许的指令或超过长度限制'
        if 'top_k' in arguments and not 1 <= arguments['top_k'] <= 5:
            return 'top_k 必须在 1 到 5 之间'
        if name in {'query_order','query_logistics','apply_refund'}:
            from app.agent.tools.mock_data import ORDERS
            order = ORDERS.get(arguments.get('order_id'))
            if not order or (order.get('tenant_id'),order.get('user_id')) != (tenant,user):
                return '订单不存在或不属于当前登录用户'
        return None

    def tool_output(self, name, output):
        try:
            payload = json.loads(output)
        except (ValueError,TypeError):
            payload = {'success':False, 'error':'工具输出不是合法 JSON'}
        if name != 'load_skill' and INJECTION.search(json.dumps(payload, ensure_ascii=False)):
            return {'success':False, 'error':'工具返回包含不可信指令，已隔离'}, {}
        evidence = {}
        if name == 'search_knowledge' and isinstance(payload,dict):
            for row in payload.get('results',[]):
                if not all(isinstance(row.get(k),str) and row[k] for k in ('doc','section','text')):
                    continue
                key = 'kb-' + sha256((row['doc']+'\n'+row['section']+'\n'+row['text']).encode()).hexdigest()[:16]
                row['citation_id'] = key
                evidence[key] = redact(row)
        return redact(payload), evidence

    def output(self, text, evidence):
        issues = []
        if UNSAFE_OUTPUT.search(text) or INJECTION.search(text):
            issues.append('unsafe_output')
        for key in re.findall(r'\[(kb-[^\]]+)\]',text):
            if key not in evidence:
                issues.append('unverified_citation')
        # Policy claims must be backed by a returned source, rather than invented source names.
        if re.search(r'根据.{0,20}(?:政策|规定|条款)',text) and not re.search(r'\[kb-[^\]]+\]',text):
            issues.append('missing_citation')
        for key, quote in re.findall(r'\[(kb-[^\]]+)\]\s*[“"]([^”"]+)[”"]',text):
            if key not in evidence or quote not in evidence[key]['text']:
                issues.append('citation_quote_mismatch')
        if issues:
            return '相关信息尚未通过核验，已转人工客服处理。', issues
        return redact(text), []
