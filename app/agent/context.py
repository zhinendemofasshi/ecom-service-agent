"""Versioned execution messages and conservative token-budget compaction."""
from __future__ import annotations
import json
from app.agent.guardrails import redact

MESSAGE_VERSION = 2


def token_upper_bound(value):
    # UTF-8 bytes upper-bound ordinary BPE tokens; include framing overhead.
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8')) + 16


def migrate_messages(messages, version=1):
    if version not in (1, 2):
        raise ValueError(f'Unsupported message schema version: {version}')
    result=[]
    for item in messages:
        if item.get('role') not in {'user','assistant','tool','system'}:
            raise ValueError('Invalid stored message role')
        result.append(redact(dict(item)))
    return result


def from_response_items(items):
    result=[]
    for item in items:
        kind=item.get('type')
        if kind=='function_call':
            result.append({'role':'assistant','content':None,'tool_calls':[{'id':item['call_id'],'type':'function',
                'function':{'name':item['name'],'arguments':item['arguments']}}]})
        elif kind=='function_call_output':
            result.append({'role':'tool','tool_call_id':item['call_id'],'content':item['output']})
        elif item.get('role') in {'user','assistant'}:
            content=item.get('content','')
            if isinstance(content,list):
                content=''.join(part.get('text','') for part in content)
            result.append({'role':item['role'],'content':content})
    return migrate_messages(result)


def compact(messages, summary, budget, summarize):
    messages=migrate_messages(messages)
    if token_upper_bound(messages)+token_upper_bound(summary or '') <= budget:
        return messages, summary, 0
    # Only cut at user-turn boundaries; tool calls and outputs stay together.
    boundaries=[i for i,m in enumerate(messages) if m['role']=='user' and i>0]
    for split in boundaries:
        recent=messages[split:]
        if token_upper_bound(recent)>budget//2:
            continue
        text=redact(summarize(messages[:split], summary))
        max_bytes=max(0, budget-token_upper_bound(recent)-128)
        text=text.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
        return recent,text,split
    raise ValueError('当前完整轮次超过上下文 token 预算，请缩短消息或开始新会话')
