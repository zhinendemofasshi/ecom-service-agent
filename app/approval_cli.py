"""CLI commands for individually scoped approval decisions."""
import json


def show_pending(agent):
    for a in agent.pending_approvals():
        print(f"\n待审批 {a['call_id']} | 订单 {a.get('order_id')} | 金额 {a.get('amount', '待核验')}")
        print(f"原因：{a.get('reason')} | 到期时间戳：{a.get('expires_at')}")


def handle_approval(command, agent, actor):
    verb,_,tail=command.partition(' ')
    if verb.lower() not in {'approve','reject','edit'}:
        return None
    actions=agent.pending_approvals()
    if not actions:
        raise RuntimeError('当前没有待审批操作')
    first,_,rest=tail.strip().partition(' ')
    if first in {a['call_id'] for a in actions}:
        call_id,body=first,rest
    elif len(actions)==1:
        call_id,body=actions[0]['call_id'],tail.strip()
    else:
        raise RuntimeError('多个操作必须指定 call_id')
    decision={'call_id':call_id,'type':verb.lower()}
    if verb.lower()=='edit':
        decision['arguments']=json.loads(body)
        if not isinstance(decision['arguments'],dict):
            raise RuntimeError('编辑内容必须为 JSON 对象')
    elif verb.lower()=='reject':
        if not body:
            raise RuntimeError('请提供拒绝原因：reject <call_id> 原因')
        decision['message']=body
    return agent.resume_decisions([decision],actor)
