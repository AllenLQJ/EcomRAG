"""仅用于路由连续解析失败后的保守兜底，不替代 LLM 分类。"""
import re

SUSPICIOUS_RULES = (
    ("other_person", r"(?:他人|别人|其他|其它|别的|另一个|另一位|另外).{0,32}(?:个人信息|隐私|地址|电话|联系|订单|姓名|身份证|邮箱)"),
    ("customer_record", r"(?:一个|一位|某个|某位|任意|所有|全部)(?:顾客|客户|用户|买家).{0,24}(?:个人信息|地址|电话|联系|订单|姓名|身份证|邮箱)"),
    ("other_person_reverse", r"(?:地址|电话|订单|个人信息).{0,24}(?:他人|别人|其他顾客|其他用户|另一位)"),
    ("hidden_prompt", r"(?:系统|隐藏|内部).{0,8}(?:提示词|指令|prompt)|system\s+prompt"),
    ("ignore_rules", r"(?:忽略|无视|绕过|不遵守).{0,16}(?:规则|指令|提示|限制)|ignore.{0,20}(?:rules|instructions)"),
    ("switch_role", r"(?:切换|改变|更换).{0,8}(?:角色|身份)|扮演|switch.{0,12}role"),
    ("medical_promise", r"(?:医疗|疗效|治疗|治病|药物|听力|疾病|健康|病情).{0,32}(?:保证|治愈|恢复|承诺|一定)"),
    ("medical_promise_reverse", r"(?:保证|治愈|恢复|承诺|一定).{0,32}(?:医疗|疗效|治疗|治病|药物|听力|疾病|健康|病情)"),
    ("cure_promise", r"(?:保证|承诺|一定).{0,12}(?:治愈|恢复|疗效)|(?:治愈|疗效).{0,12}(?:保证|承诺)"),
    ("legal_conclusion", r"法律.{0,12}(?:结论|保证|承诺)"),
)


def is_suspicious(question):
    return any(re.search(pattern, question, re.IGNORECASE | re.DOTALL) for _, pattern in SUSPICIOUS_RULES)
