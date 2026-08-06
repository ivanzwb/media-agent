from app.pipeline.diversion import check_draft, scan_text


def kinds(findings):
    return [f["kind"] for f in findings]


def texts(findings):
    return [f["text"] for f in findings]


# ── 微信号 ────────────────────────────────────────────────────────────────

def test_a_wechat_id_is_caught_however_it_is_written():
    for line in ("vx: helloworld9", "微信号：helloworld9", "加微信 helloworld9",
                 "威信 helloworld9", "V信helloworld9", "微信 = helloworld9"):
        found = scan_text(line)
        assert kinds(found) == ["wechat_id"], line


def test_talking_about_wechat_is_not_a_wechat_id():
    """词表把「微信」当禁词会误伤正常报道，模式匹配不能重蹈覆辙。"""
    body = (
        "微信在 2026 年 3 月更新了条款。\n"
        "WeChat Official Accounts 的推荐机制变了。\n"
        "腾讯的 wx.qq.com 页面写得很清楚。\n"
        "微信支付分成两档。\n"
    )
    assert scan_text(body) == []


def test_one_line_reports_one_wechat_id():
    found = scan_text("加微信 helloworld9 或 vx: backupid123")
    assert len(found) == 1


def test_a_wechat_id_in_the_digest_counts_too():
    """摘要就是卡片上的简介，出现在那里一样掉推荐。"""
    findings = check_draft({"digest": "领取资料请加微信 helloworld9",
                            "body_md": "正文"})
    assert kinds(findings) == ["wechat_id"]
    assert findings[0]["where"] == "摘要"


# ── 二维码图片 ─────────────────────────────────────────────────────────────

def test_a_qr_picture_is_caught_by_its_name():
    findings = scan_text("![](https://cdn.x.com/qrcode-2.png)")
    assert kinds(findings) == ["qr_image"]


def test_a_qr_picture_is_caught_by_the_words_around_it():
    """文件名看不出来的时候，「长按识别」就说明了那张图是什么。"""
    findings = scan_text("![](media/img-7.png) 长按识别关注我们")
    assert kinds(findings) == ["qr_image"]
    assert texts(findings) == ["media/img-7.png"]


def test_a_qr_caption_on_the_next_line_still_counts():
    """公众号的常见排法是图一行、「长按识别」另起一行。"""
    findings = scan_text("![](media/img-9.png)\n长按识别二维码关注\n")
    assert kinds(findings) == ["qr_image"]
    assert findings[0]["line"] == 1


def test_an_ordinary_picture_passes():
    assert scan_text("![模型架构图](media/img-2.png)") == []


def test_an_html_qr_image_is_caught():
    findings = scan_text('<img src="media/erweima.jpg" width="200">')
    assert kinds(findings) == ["qr_image"]


# ── 站外链接 ──────────────────────────────────────────────────────────────

def test_outbound_links_are_reported_in_the_order_they_appear():
    findings = scan_text("详情见 https://example.com/post 和 [官网](https://foo.cn/x)")
    assert kinds(findings) == ["external_link", "external_link"]
    assert texts(findings) == ["https://example.com/post", "https://foo.cn/x"]


def test_a_picture_url_is_not_an_outbound_link():
    """配图发布前会被传成微信素材，报成外链只会让体检没人看。"""
    assert scan_text("![图](https://cdn.x.com/photo.png)") == []


def test_a_video_platform_url_is_not_an_outbound_link():
    """视频整段嵌入播放，平台不把它当引流，报出来只会吓人。"""
    for line in ("[演示](https://www.youtube.com/watch?v=dQw4w9WgXcQ)",
                 "https://www.bilibili.com/video/BV1xx411c7mD",
                 "<iframe src=\"https://player.vimeo.com/video/123\">"):
        assert scan_text(line) == [], line


def test_a_direct_video_file_is_not_an_outbound_link():
    """直链视频和配图一样会被当成素材上传，不是站外链接。"""
    for line in ("[素材](https://cdn.x.com/clip.mp4)",
                 "https://cdn.x.com/live.m3u8"):
        assert scan_text(line) == [], line


def test_a_link_back_into_wechat_is_not_outbound():
    assert scan_text("往期：https://mp.weixin.qq.com/s/abc") == []


def test_a_url_inside_a_code_fence_is_left_alone():
    body = "```bash\ncurl https://api.example.com/v1\n```\n正文\n"
    assert scan_text(body) == []


def test_a_finding_points_at_the_line_it_came_from():
    body = "第一行\n第二行\n加微信 helloworld9\n"
    assert scan_text(body)[0]["line"] == 3


# ── 推广文案 ──────────────────────────────────────────────────────────────

def test_the_promotion_footer_is_checked_on_its_own():
    """推广文案是用户自填的自由文本，可能还没落进正文。"""
    findings = check_draft({"body_md": "干净的正文"},
                           promotion_footer="关注我们，加微信 helloworld9")
    assert kinds(findings) == ["wechat_id"]
    assert findings[0]["where"] == "推广文案"


def test_the_footer_already_in_the_body_is_not_reported_twice():
    footer = "关注我们，加微信 helloworld9"
    findings = check_draft({"body_md": f"正文\n\n---\n{footer}\n"},
                           promotion_footer=footer)
    assert len(findings) == 1
    assert findings[0]["where"] == "正文"


def test_a_clean_draft_reports_nothing():
    assert check_draft({
        "title_cn": "微信改了推荐规则，你的文章还能被看到吗",
        "digest": "完读率成了最重要的信号，低于三成的文章会掉出推荐。",
        "body_md": "## 一、开头\n![图](media/img-1.png)\n正文内容。\n",
    }) == []
