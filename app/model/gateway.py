from __future__ import annotations

from types import SimpleNamespace


def convert_messages(messages):
    instructions, items = [], []
    for message in messages:
        role, content = message.get('role'), message.get('content')
        if role in {'system', 'developer'}:
            if content:
                instructions.append(str(content))
            continue
        if role in {'user', 'assistant'} and content:
            items.append({'role': role, 'content': str(content)})
        for call in message.get('tool_calls') or []:
            items.append({'type':'function_call', 'call_id':call['id'],
                          'name':call['function']['name'], 'arguments':call['function']['arguments']})
        if role == 'tool':
            items.append({'type':'function_call_output', 'call_id':message['tool_call_id'], 'output':str(content or '')})
    return '\n\n'.join(instructions), items


class ModelGateway:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def options(request):
        request = dict(request)
        request['store'] = False
        if str(request.get('model', '')).startswith(('gpt-5', 'o1', 'o3', 'o4')):
            request.pop('temperature', None)
        return request

    def create(self, **request):
        response = self.client.responses.create(**self.options(request))
        if getattr(response, 'status', None) in {'failed', 'incomplete', 'cancelled'}:
            raise RuntimeError('模型未返回完整结果')
        return response

    def stream(self, **request):
        return self.client.responses.stream(**self.options(request))

    def text(self, *, model, messages, temperature=0, max_output_tokens=2048):
        from app.agent.guardrails import redact
        instructions, items = convert_messages(redact(messages))
        return redact(self.create(model=model, instructions=instructions, input=items,
                                 temperature=temperature, max_output_tokens=max_output_tokens).output_text or '')

    def chat(self, *, model, messages, temperature=0, tools=None, max_tokens=None,
             response_format=None, reasoning_effort=None, **kwargs):
        """Compatibility shape for offline evaluators; transport is always Responses."""
        from app.agent.guardrails import redact
        instructions, items = convert_messages(redact(messages))
        request = dict(model=model, instructions=instructions, input=items, temperature=temperature,
                       max_output_tokens=max(32, max_tokens or 4096))
        if tools:
            request['tools'] = [dict(type='function', **t['function'], strict=False) for t in tools]
        schema = response_format if isinstance(response_format, type) else None
        if schema:
            # Schema-guided fallback also supports compatible Responses providers without parse().
            request['instructions'] += '\nReturn JSON matching this schema:\n' + str(schema.model_json_schema())
        response = self.create(**request)
        raw = redact(response.output_text or '')
        parsed = None
        if schema:
            parsed = schema.model_validate_json(raw.removeprefix('```json').removeprefix('```').removesuffix('```').strip())
        calls = [SimpleNamespace(id=i.call_id, type='function', function=SimpleNamespace(name=i.name, arguments=i.arguments))
                 for i in response.output if i.type == 'function_call']
        usage = getattr(response, 'usage', None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw, parsed=parsed, tool_calls=calls))],
            usage=SimpleNamespace(prompt_tokens=getattr(usage,'input_tokens',0), completion_tokens=getattr(usage,'output_tokens',0),
                                  total_tokens=getattr(usage,'total_tokens',0)))

    chat_parse = chat
