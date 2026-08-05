from app.pipeline.ai_flavor import check_text


def points(result, key):
    for dim in result["dimensions"]:
        if dim["key"] == key:
            return dim["points"]
    return 0.0


def keys(result):
    return {d["key"] for d in result["dimensions"]}


# 一段人写的稿子：有具体的人、数字和一处转折，没有套话。
CLEAN = (
    "# 我把构建从 14 分钟压到了 90 秒\n\n"
    "上周三我提了个只改一行注释的 PR，眼睁睁看着 CI 跑了十四分钟。\n\n"
    "翻日志才发现，九分钟花在装依赖上。缓存配了，命中率只有 3%，"
    "因为 key 里带了时间戳。这行是两年前一个已经离职的同事加的。\n\n"
    "改掉之后掉到 6 分钟。剩下的大头是测试，2300 个，全串行。\n\n"
    "拆并行失败了两次，有一批测试共享同一个 sqlite 文件。我最后给每个 worker "
    "分了独立临时目录，磁盘占用翻四倍。丑，但能用。\n\n"
    "现在是 90 秒。还能更快吗？大概能。但我不想碰了。\n"
)

# 同一个话题，写成模型最爱的样子。
SLOP = (
    "# 🚀 构建优化的下半场\n\n"
    "近年来，随着工程效能的快速发展，构建速度正在深刻改变研发体验。"
    "在当今这个时代背景下，说实话，你会发现这场变革来得比想象中更快。\n\n"
    "## 三大趋势\n\n"
    "构建优化不仅仅是速度的比拼，更是工程文化的较量。表面上是缓存之争，"
    "本质上是流程、工具和人才的综合博弈。\n\n"
    "- **缓存命中：** 持续提升，形成闭环。\n"
    "- **并行执行：** 显著降低耗时，降本增效。\n"
    "- **工具链：** 不断丰富，赋能全链路。\n\n"
    "此外，值得注意的是，生态正在加速成熟。与此同时，团队也在调整策略。"
    "因此，未来仍存在巨大变数。换句话说，这是充满不确定性的阶段。\n\n"
    "## 写在最后\n\n"
    "总的来说，比拼的不再是谁的机器更快，而是谁能真正解决问题。"
    "在通往极致效能的道路上，我们仍需保持敬畏。愿我们都能找到自己的位置，"
    "未来可期。\n"
)


def test_a_human_draft_barely_registers():
    result = check_text(CLEAN)
    assert result["score"] < 20
    assert result["level"] == "很淡"


def test_the_same_topic_written_by_a_model_scores_heavy():
    result = check_text(SLOP)
    assert result["score"] >= 70
    assert result["level"] == "很重"
    # 命中的不该只是某一类，套路是成片出现的。
    assert {"opening", "fake_depth", "mechanical", "elevation"} <= keys(result)


def test_one_stray_cliche_is_not_worth_much():
    """单点不算数：一处套话只该扣个零头，否则正常稿子全成了 AI。"""
    result = check_text(CLEAN.replace("丑，但能用。", "丑，但这套流程已经形成闭环。"))
    assert points(result, "jargon") <= 4
    assert result["score"] < 20


def test_piling_the_same_cliche_up_does_cost():
    """同一个毛病反复出现才是证据，扣分要跟着涨。"""
    once = check_text(CLEAN + "\n这套打法形成闭环。\n")
    many = check_text(CLEAN + "\n这套打法形成闭环，靠的是赋能、抓手和顶层设计，"
                              "颗粒度很细，底层逻辑清晰，全链路降本增效。\n")
    assert points(many, "jargon") > points(once, "jargon") * 2


def test_a_hit_points_at_the_line_it_is_on():
    result = check_text("第一行是正常的句子。\n第二行也很正常。\n愿我们都能走到最后。\n")
    hit = [f for f in result["findings"] if f["key"] == "wechat"]
    assert hit and hit[0]["line"] == 3


def test_findings_come_back_in_reading_order():
    result = check_text(SLOP)
    lines = [f["line"] for f in result["findings"]]
    assert lines == sorted(lines)


def test_cliches_inside_a_code_block_are_not_the_author_talking():
    fenced = ("正常的一句话。\n\n"
              "```python\n"
              "# 近年来，随着业务的发展，这里需要赋能\n"
              "SLOGAN = '愿我们都能形成闭环'\n"
              "```\n\n"
              "另一句正常的话。\n")
    assert check_text(fenced)["findings"] == []


def test_component_syntax_and_urls_are_not_prose():
    """::: 是本项目的组件标记，链接地址也不是写给读者读的句子。"""
    text = (":::tip 赋能\n"
            "参考 https://example.com/近年来-赋能-闭环 这个页面。\n"
            ":::\n")
    assert check_text(text)["findings"] == []


def test_evenly_measured_sentences_get_called_out():
    """人写东西长短句混着来，一路匀速输出是机器的习惯。"""
    even = "".join(f"这是第{i}个长度几乎完全一样的句子内容。\n" for i in range(12))
    assert points(even_result := check_text(even), "rhythm") > 0
    assert any(d["key"] == "rhythm" and d.get("note")
               for d in even_result["dimensions"])


def test_a_couple_of_sentences_are_too_few_to_judge_rhythm():
    assert points(check_text("短句。\n这是稍微长一点的第二句话。\n"), "rhythm") == 0


def test_an_empty_draft_scores_zero_instead_of_exploding():
    for text in ("", "   \n\n", None):
        result = check_text(text)
        assert result["score"] == 0
        assert result["findings"] == []


def test_a_flood_of_hits_is_summarised_not_dumped():
    """命中几十处时，清单只列前几条，但计数要如实。"""
    result = check_text("愿我们都能坚持下去。\n" * 40)
    dim = [d for d in result["dimensions"] if d["key"] == "wechat"][0]
    assert dim["hits"] == 40
    assert dim["shown"] == 12
    assert len([f for f in result["findings"] if f["key"] == "wechat"]) == 12


def test_the_score_never_runs_past_a_hundred():
    result = check_text(SLOP * 6)
    assert result["score"] == 100
    assert result["level"] == "很重"
