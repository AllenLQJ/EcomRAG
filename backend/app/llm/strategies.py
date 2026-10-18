def build_queries(decision, question):
    # TODO(P1): 实现 hyde/subquery/stepback；当前所有策略均降级为 direct。
    return [decision.rewritten_query.strip() or question]
