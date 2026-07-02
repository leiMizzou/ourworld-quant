"""学习闭环(learning loop)层: 常量、渲染 helper 与 LearningMixin。

从 server.py 拆出; AppHandler 通过继承 LearningMixin 获得全部 learning_* /
handle_learning_* / render_learn* 方法。本模块不得在顶层 import server(依赖方向: server -> learning);
个别依赖 server 运行时状态的入口(csrf_input/SECRET)通过调用时延迟导入获取。
"""
from __future__ import annotations

import re
from html import escape
from urllib.parse import parse_qs, quote, urlparse

from . import services
from .ai import service as ai_service
from .render_helpers import (
    labeled_empty_row,
    labeled_table_row,
    money,
    render_markdown,
)

TRUE_VALUES = {"1", "true", "yes", "on"}


def csrf_input(user) -> str:
    # SECRET 可被 refresh_runtime_secret() 重绑, 必须在调用时从 server 模块取
    from . import server

    return server.csrf_input(user)


def _runtime_secret() -> str:
    from . import server

    return server.SECRET


LEARNING_PRESETS = (
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "reversal",
        "title": "量化投资到底是什么?",
        "summary": "先弄清楚数据、规则、策略和模拟盘分别在做什么。",
        "goal": "我是零基础,想先理解量化投资到底是什么,它和主观投资、AI聊天选股有什么区别,并用一个最简单的模拟盘练习建立基本概念。",
    },
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "reversal",
        "title": "我该先学哪些基础?",
        "summary": "按最短路径梳理行情、交易规则、回测陷阱和复盘。",
        "goal": "我是新手,想知道学习量化投资最先应该掌握哪些基础知识,每个知识点为什么重要,以及如何用模拟盘做一次安全的小练习。",
    },
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "momentum",
        "title": "AI 能帮我做什么?",
        "summary": "理解 AI 适合做拆解、解释、记录,不适合替你下判断。",
        "goal": "我想学习 AI 在量化投资学习中能帮我做什么,哪些事情不能让 AI 替我决定,并设计一个用 AI 辅助记录和复盘的入门练习。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "reversal",
        "title": "做一次反转观察",
        "summary": "观察短期跌幅靠前的候选,学习观察想法、数量边界和复盘记录。",
        "goal": "我想通过一次反转观察练习,学习什么是观察想法、候选筛选、数量边界和复盘记录,不要追求收益,重点学习如何验证一个想法。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "momentum",
        "title": "做一次动量观察",
        "summary": "观察短期强势候选,理解趋势、回撤和过拟合风险。",
        "goal": "我想做一次动量观察练习,学习如何看趋势信号、如何避免追涨冲动、如何设置观察指标和复盘问题。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "prediction",
        "title": "理解模型预测候选",
        "summary": "把预测结果当成学习材料,而不是买卖指令。",
        "goal": "我想学习如何理解模型预测候选,知道预测值、行情、风险记录和模拟盘验证之间是什么关系,并设计一次只用于学习的预测候选观察练习。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "risk_review",
        "title": "如何控制风险?",
        "summary": "从仓位、回撤、交易成本和停止条件建立风险框架。",
        "goal": "我想系统学习量化练习里的风险控制,包括仓位、回撤、交易成本、停止继续练习的条件,并形成一套可以复盘的检查清单。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "risk_review",
        "title": "如何复盘模拟盘?",
        "summary": "把成交、持仓和演练依据转成可学习的复盘问题。",
        "goal": "我已经知道模拟盘只是训练,想学习如何复盘自己的成交、持仓、演练依据和结果,找出方法上的问题而不是只看赚亏。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "prediction",
        "title": "怎么避免过拟合?",
        "summary": "学习样本内外、未来函数、幸存者偏差和成本低估。",
        "goal": "我想学习量化研究里常见的过拟合和回测陷阱,包括未来函数、幸存者偏差、样本内外和成本低估,并设计一个模拟盘层面的检查练习。",
    },
)
LEARNING_DEMO_COACH_MARKDOWN = """
### 目标拆解
1. 先把“量化投资”理解成一套可记录、可复盘的学习流程,不是让 AI 替你判断涨跌。
2. 第一次练习只观察少量示例对象,重点看数据、规则、依据和风险边界。
3. 练习完成后不要先问赚没赚钱,先问自己是否说清楚了为什么观察、观察什么、什么时候停止。

### 你需要掌握的 3 个概念
- **数据**: 你看到的价格、涨跌和来源日期,只是练习材料。
- **规则**: 系统把一个想法变成固定步骤,这样以后才可以复盘。
- **复盘**: 记录想练什么、有没有按规则做、下次先改哪一点。

### 第一次练习的边界
- 只做模拟训练,不产生真实委托。
- 每个对象只用最小练习数量。
- 这不是买卖建议,只是告诉你系统会怎样把学习目标变成可观察的练习。

### 下一步
先看下面的练习草稿,确认你能看懂每一列在说什么;真正登录后,系统才会把类似草稿保存到你的模拟盘里。
"""
LEARNING_DEMO_ROWS = (
    {
        "name": "示例指数基金 A",
        "action": "观察 100 份模拟数量",
        "why": "价格波动相对容易理解,适合练习看“目标-记录-复盘”。",
    },
    {
        "name": "示例行业基金 B",
        "action": "观察 100 份模拟数量",
        "why": "用来比较不同对象的涨跌和风险差异,不追求短期结果。",
    },
    {
        "name": "示例股票 C",
        "action": "观察 100 股模拟数量",
        "why": "学习单只股票波动更大时,为什么要先写清楚风险边界。",
    },
)




def learning_display_rationale(value: object) -> str:
    """Keep learning pages focused on the observation reason, not data plumbing."""
    text = " ".join(str(value or "").split())
    if not text:
        return "记录观察依据、风险边界和复盘问题。"
    text = re.sub(r"\s*·\s*入场价.*$", "", text)
    text = re.sub(r"[，,]\s*来源\s*[^|。；;]+", "", text)
    text = re.sub(r"\s*\|\s*", "。", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ，,;；。")
    if not text:
        return "记录观察依据、风险边界和复盘问题。"
    if "不是买卖" not in text and "不构成投资建议" not in text:
        text = f"{text}。这只是模拟观察依据,不是买卖建议。"
    return text


def learning_observation_action(side: str, qty: int) -> str:
    verb = "观察" if side == "buy" else "退出观察"
    return f"{verb} {int(qty)}"


def learning_observation_label(side: str) -> str:
    return "模拟观察" if side == "buy" else "退出观察"




class LearningMixin:
    def learning_notice_html(self, msg: str) -> str:
        clean = " ".join(str(msg or "").split())
        if not clean:
            return ""
        notice: tuple[str, str, str, str, str] | None = None
        if (
            "先从学习工作台开始" in clean
            or "账号已就绪" in clean
            or "注册成功" in clean
            or "扫码注册成功" in clean
        ):
            notice = (
                "0/6",
                "欢迎来到学习工作台",
                "现在不用找菜单、配置 key 或写提示词。先看一句话,再点蓝色推荐按钮开始第一关。",
                "#learn-presets",
                "开始第一关",
            )
        elif "下一步点击“一键生成今日练习”" in clean:
            notice = (
                "3/6",
                "目标已经建好",
                "刚才已经完成选目标和教练拆解。现在不用读完全文,只点蓝色按钮生成 1 条今日练习。",
                "#task-next-action",
                "生成今日练习",
            )
        elif "今日练习" in clean and ("已保存" in clean or "生成" in clean or "草稿" in clean):
            notice = (
                "4/6",
                "今日练习已经生成",
                "刚才只是保存了待观察练习,还没有成交。下一步回到今日练习,确认材料、数量和依据后生成观察记录。",
                "#today-practice",
                "去生成观察记录",
            )
        elif "模拟观察记录已生成" in clean or "已开始模拟观察" in clean or "来自学习任务,先回学习页完成复盘" in clean:
            notice = (
                "5/6",
                "观察记录已经生成",
                "刚才生成的是模拟观察记录。现在不用判断赚亏,先一键完成 6/6 并保存示例复盘。",
                "#learning-review",
                "完成 6/6",
            )
        elif "解锁 6/6" in clean or "学习闭环完成" in clean:
            notice = (
                "6/6",
                "第一次学习闭环完成",
                "复盘已经保存。今天可以停在这里;下次回来先看学习轨迹里的“下次改什么”。",
                "#learning-journey",
                "查看学习轨迹",
            )
        if notice is None:
            return ""
        step, title, text, href, label = notice
        action_html = f'<a class="btn blue" href="{escape(href, quote=True)}">{escape(label)}</a>'
        if step == "0/6":
            user = self.current_user()
            if user:
                action_html = (
                    '<form method="post" action="/learn/sample-task">'
                    f"{csrf_input(user)}"
                    '<input type="hidden" name="preset" value="0">'
                    '<input type="hidden" name="quick_start" value="1">'
                    '<button class="btn blue" type="submit">一键开始第一关</button>'
                    "</form>"
                )
        elif step == "3/6":
            user = self.current_user()
            path = urlparse(self.path).path
            if user and path.startswith("/learn/tasks/"):
                try:
                    task_id = self.learning_task_id_from_path(path)
                except (TypeError, ValueError):
                    task_id = 0
                if task_id > 0:
                    action_html = (
                        f'<form method="post" action="/learn/tasks/{task_id}/quick-save">'
                        f"{csrf_input(user)}"
                        '<button class="btn blue" type="submit">一键生成今日练习</button>'
                        "</form>"
                    )
        return f"""
<div class="msg learning-notice" role="status">
  <div>
    <span class="tag">{escape(step)}</span>
    <strong>{escape(title)}</strong>
    <p>{escape(text)}</p>
    <small>{escape(clean)}</small>
  </div>
  {action_html}
</div>
"""

    def learning_difficulty_options(self, selected: str = "beginner") -> str:
        current = services.normalize_learning_difficulty(selected)
        return "".join(
            f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
            for value, label in services.LEARNING_DIFFICULTIES.items()
        )

    def learning_template_options(self, selected: str = "reversal", include_risk: bool = True) -> str:
        current = services.normalize_learning_template(selected)
        items = services.LEARNING_TEMPLATES.items()
        if not include_risk:
            items = [(value, label) for value, label in items if value != "risk_review"]
        return "".join(
            f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
            for value, label in items
        )

    def learning_promise_strip_html(self, items: tuple[tuple[str, str], ...]) -> str:
        item_html = "".join(
            f"<div><b>{escape(title)}</b><span>{escape(text)}</span></div>"
            for title, text in items
        )
        return f'<div class="loop-promise">{item_html}</div>'

    def learning_preset_by_index(self, raw: str | int | None) -> tuple[int, dict]:
        try:
            idx = int(str(raw if raw is not None else "0").strip())
        except ValueError:
            idx = 0
        if idx < 0 or idx >= len(LEARNING_PRESETS):
            idx = 0
        return idx, LEARNING_PRESETS[idx]

    def sample_learning_task_template(self, preset: dict) -> str:
        template = services.normalize_learning_template(preset.get("template") or "reversal")
        return "reversal" if template == "risk_review" else template

    def learning_task_practice_template(self, task) -> str:
        template = services.normalize_learning_template(task["template"] or "reversal")
        return "reversal" if template == "risk_review" else template

    def learning_task_coach_label(self, task) -> str:
        coach_text = str(task["coach_text"] or "")
        if "示例教练" in coach_text or "AI 教练暂时不可用" in coach_text:
            return "示例教练"
        return "AI 教练"

    def sample_learning_coach_text(self, preset: dict) -> str:
        template = services.normalize_learning_template(preset.get("template") or "reversal")
        template_label = services.LEARNING_TEMPLATES.get(template, template)
        if template == "risk_review":
            practice_line = (
                "先把风险拆成 3 个小问题: **数量边界、回撤边界、停止条件**;生成今日练习时,"
                "系统会用一条小数量观察作为材料,重点不是买卖,而是给练习补上边界。"
            )
        else:
            practice_line = f"先用 **{template_label}** 做小数量模拟观察,重点看依据、数量和风险边界。"
        return f"""
### 示例教练拆解
你选择的是: **{preset['title']}**。

这不是 DeepSeek 实时生成的回答,而是系统内置的示例教练。它的目标是让你先跑通第一次学习闭环:理解一个概念,生成今日练习,生成观察记录,再保存三问复盘。

### 这次先学什么
1. 把“量化投资”理解成一套可记录、可复盘的学习流程,不是让 AI 替你预测涨跌。
2. {practice_line}
3. 练习完成后先问“我想练什么、有没有按小数量规则做、下次先改哪一点”,不要先问短期赚亏。

### 练习边界
- 只做模拟训练,不产生真实交易。
- 候选由系统按现有行情/预测数据生成,不是模型自由荐股。
- 保存后只会出现在学习页的「今日练习」里,你仍然要自己确认后才会生成观察记录。

### 下一步
先点击“一键生成今日练习”。系统只会保存为待观察计划,不会自动成交;想自己调参数时,再展开进阶草稿设置。
"""

    def fallback_learning_coach_text(self, goal: str, difficulty: str, template: str, reason: str) -> str:
        preset = {
            "title": "你的自定义学习目标",
            "goal": goal,
            "difficulty": services.normalize_learning_difficulty(difficulty),
            "template": services.normalize_learning_template(template),
        }
        safe_reason = " ".join(str(reason or "AI 服务暂时不可用。").split())[:180]
        return f"""
### AI 教练暂时不可用,先用示例教练继续
DeepSeek 这次没有返回可用拆解: {safe_reason}

为了不让第一次学习闭环卡在外部模型问题上,系统已改用内置示例教练。你仍然可以继续完成同一个流程:看拆解、生成今日练习、生成观察记录、保存三问复盘。

{self.sample_learning_coach_text(preset)}
"""

    def learning_preset_cards(self, user, ai_ready: bool) -> str:
        stages = (
            ("第 1 关 · 先懂概念", "完全不懂也从这里开始。第一次优先点第一个推荐题,不要先研究所有菜单。", "适合 0-3 分钟"),
            ("第 2 关 · 做一次观察", "完成第一圈 6/6 后再来。这里开始练反转、动量和预测候选,仍然只做模拟观察。", "完成第一关后"),
            ("第 3 关 · 风险和复盘", "有第一条复盘记录后再看。重点是风险边界、复盘习惯和常见研究陷阱。", "有复盘后"),
        )
        cards_by_stage: dict[str, list[str]] = {title: [] for title, _, _ in stages}
        for idx, preset in enumerate(LEARNING_PRESETS):
            if str(preset["difficulty"]) == "beginner":
                stage_title = stages[0][0]
            elif str(preset["difficulty"]) == "balanced":
                stage_title = stages[1][0]
            else:
                stage_title = stages[2][0]
            chips = (
                f'<span class="badge">{escape(preset["level"])}</span>'
                f'<span class="badge">{escape(services.LEARNING_TEMPLATES[preset["template"]])}</span>'
            )
            if idx == 0:
                stage_hint = '<span class="quest-start">推荐第一关:完全不懂就点这个</span>'
                cta_line = "<p><strong>点这里开始第 1 关</strong></p>"
            elif str(preset["difficulty"]) == "beginner":
                stage_hint = '<span class="quest-start">第 1 关可选题:想换一个概念再点</span>'
                cta_line = "<p><strong>点这里创建学习任务</strong></p>"
            else:
                stage_hint = '<span class="quest-lock">建议完成当前 6/6 后再点</span>'
                cta_line = "<p><strong>完成第一圈后再创建</strong></p>"
            outcome = (
                '<div class="choice-outcome">'
                f"<span><b>点击后</b>{'让 AI 拆解这个目标' if ai_ready else '创建一个示例教练任务'}</span>"
                "<span><b>下一步</b>生成 1 条今日练习</span>"
                "<span><b>安全边界</b>不会自动成交</span>"
                "</div>"
            )
            content = (
                f"{chips}"
                f"<strong>{escape(preset['title'])}</strong>"
                f"<p>{escape(preset['summary'])}</p>"
                f"{outcome}"
                f"{stage_hint}"
                f"{cta_line}"
            )
            if ai_ready:
                cards_by_stage[stage_title].append(
                    '<form class="preset-form" method="post" action="/learn/coach">'
                    f"{csrf_input(user)}"
                    f'<input type="hidden" name="goal" value="{escape(preset["goal"], quote=True)}">'
                    f'<input type="hidden" name="difficulty" value="{escape(preset["difficulty"])}">'
                    f'<input type="hidden" name="template" value="{escape(preset["template"])}">'
                    f'<button type="submit" class="preset-card">{content}</button>'
                    "</form>"
                )
            else:
                quick_start_input = '<input type="hidden" name="quick_start" value="1">' if idx == 0 else ""
                cards_by_stage[stage_title].append(
                    '<form class="preset-form" method="post" action="/learn/sample-task">'
                    f"{csrf_input(user)}"
                    f'<input type="hidden" name="preset" value="{idx}">'
                    f"{quick_start_input}"
                    f'<button type="submit" class="preset-card">{content}'
                    '<p><strong>创建示例任务,不用 DeepSeek key。</strong></p>'
                    '</button>'
                    "</form>"
                )
        stage_html = []
        for stage_title, stage_text, stage_meta in stages:
            cards = "".join(cards_by_stage[stage_title])
            if not cards:
                continue
            stage_html.append(
                '<section class="quest-stage">'
                '<div class="quest-stage-head">'
                f'<div><span class="tag">QUEST MAP</span><strong>{escape(stage_title)}</strong><p>{escape(stage_text)}</p></div>'
                f'<span class="quest-stage-meta">{escape(stage_meta)}</span>'
                '</div>'
                f'<div class="preset-grid">{cards}</div>'
                '</section>'
            )
        return '<div class="quest-ladder" aria-label="分级学习任务地图">' + "".join(stage_html) + "</div>"

    def learning_starter_choice_form(
        self,
        user,
        ai_ready: bool,
        preset_idx: int,
        heading: str,
        summary: str,
        recommended: bool = False,
    ) -> str:
        idx, preset = self.learning_preset_by_index(preset_idx)
        button_class = "starter-choice"
        form_id = ""
        chips = (
            f'<span class="badge">{escape("推荐起步" if recommended else preset["level"])}</span> '
            f'<span class="badge">{escape(services.LEARNING_TEMPLATES[preset["template"]])}</span>'
        )
        outcome = (
            '<div class="choice-outcome">'
            f"<span><b>点击后</b>{'让 AI 教练拆解目标' if ai_ready else '创建示例教练任务'}</span>"
            "<span><b>下一步</b>生成 1 条今日练习</span>"
            "<span><b>安全边界</b>不会自动成交</span>"
            "</div>"
        )
        cta_text = "点这里创建学习任务" if not recommended else "推荐第一关"
        mode_text = "AI 教练会拆解" if ai_ready else "不用 key,直接创建示例任务"
        content = (
            f"{chips}"
            f"<strong>{escape(heading)}</strong>"
            f"<p>{escape(summary)}</p>"
            f"{outcome}"
            f"<small>{escape(cta_text)} · {escape(mode_text)}</small>"
        )
        if ai_ready:
            return (
                f'<form class="starter-form" method="post" action="/learn/coach"{form_id}>'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="goal" value="{escape(preset["goal"], quote=True)}">'
                f'<input type="hidden" name="difficulty" value="{escape(preset["difficulty"])}">'
                f'<input type="hidden" name="template" value="{escape(preset["template"])}">'
                f'<button type="submit" class="{button_class}">{content}</button>'
                "</form>"
            )
        return (
            f'<form class="starter-form" method="post" action="/learn/sample-task"{form_id}>'
            f"{csrf_input(user)}"
            f'<input type="hidden" name="preset" value="{idx}">'
            f'<button type="submit" class="{button_class}">{content}</button>'
            "</form>"
        )

    def learning_starter_choices_html(self, user, ai_ready: bool, has_tasks: bool) -> str:
        if has_tasks:
            return ""
        mode_text = (
            "AI 教练已配置,但第一圈蓝色按钮仍先用内置示例教练,更快完成闭环;想自己写目标时再展开 AI 输入框。"
            if ai_ready
            else "没配置 DeepSeek key 也能开始,点击后创建内置示例教练任务,不调用 DeepSeek、不产生 AI 费用。"
        )
        fast_action = (
            '<form method="post" action="/learn/sample-task">'
            f"{csrf_input(user)}"
            '<input type="hidden" name="preset" value="0">'
            '<input type="hidden" name="quick_start" value="1">'
            '<button class="blue" type="submit">一键开始第一关</button>'
            "</form>"
        )
        return f"""
<section class="card starter-card" id="learn-presets">
  <div class="starter-head">
    <div>
      <span class="tag">从这里开始</span>
      <strong>第一步:先懂一句话,再一键开始</strong>
      <p>不用先写提示词,也不用理解所有菜单。先用 30 秒知道这里不是荐股工具,再点蓝色按钮开始第一次学习闭环。</p>
    </div>
    <p class="muted">{escape(mode_text)}</p>
  </div>
  <div class="starter-fast-path">
    <div><b>现在只做一件事</b><span>先记住一句话:量化投资不是猜涨跌,而是把想法写成规则,再用模拟记录检查。然后点“一键开始第一关”。</span></div>
    {fast_action}
  </div>
  <div class="task-action-points starter-primer">
    <div><b>30 秒先懂一句话</b><p>量化投资不是猜涨跌,而是把想法写成规则,再用数据和模拟记录检查。</p></div>
    <div><b>AI 在这里像教练</b><p>它帮你解释、拆解和复盘,不替你决定买卖,也不会自动成交。</p></div>
    <div><b>第一圈只求完成</b><p>点一键开始,生成 1 条练习,生成观察记录后保存三问复盘。</p></div>
  </div>
  {self.learning_promise_strip_html((
    ("3-5 分钟", "只跑通第一次闭环,不要求学完所有概念。"),
    ("无 key 也能开始", "没有 DeepSeek key 时使用示例教练,不消耗额度。"),
    ("不会真实交易", "只创建学习任务和模拟练习,不会自动成交。"),
    ("完成有反馈", "保存复盘后会解锁 6/6 和第一枚学习徽章。"),
  ))}
  <div class="starter-selected">
    <div><b>推荐第一关</b><span>我完全不懂,先从概念开始。</span></div>
    <div><b>点击后发生什么</b><span>创建示例教练任务,并直接准备 1 条今日练习。</span></div>
    <div><b>安全边界</b><span>不会扣 AI 费用,不会自动成交,下一步只确认观察材料。</span></div>
  </div>
</section>
"""

    def learning_beginner_focus_html(
        self,
        has_tasks: bool,
        reflection_count: int,
        continue_task_html: str,
        today_practice_html: str,
        recent_review_html: str,
    ) -> str:
        if reflection_count > 0:
            tag = "FIRST BADGE"
            title = "第一枚学习徽章已解锁"
            text = "你已经完成一次从目标、拆解、练习、观察到复盘的闭环。今天可以停在这里,下次回来先看学习轨迹。"
            href = "#learning-journey"
            label = "查看我的学习轨迹"
            steps = (
                ("已完成", "目标、练习、观察记录和复盘已经串起来。"),
                ("今天可以停", "第一圈已经达标,不用马上继续。"),
                ("下次回来", "先看学习轨迹里的“下次改什么”。"),
            )
        elif recent_review_html:
            tag = "ONLY STEP"
            title = "小白模式:最后 30 秒完成 6/6"
            text = "现在不用判断赚亏,也不用写长分析。点“一键完成 6/6”保存示例复盘,第一圈就完成。"
            href = "#learning-review"
            label = "去完成 6/6"
            steps = (
                ("只答三问", "想练什么、有没有按规则做、下次改哪一点。"),
                ("可以用示例", "不会写就先保存示例,之后还能修改。"),
                ("完成标志", "页面显示 6/6 和学习徽章。"),
            )
        elif today_practice_html:
            tag = "ONLY STEP"
            title = "小白模式:现在只生成观察记录"
            text = "不要去高级模拟盘,也不要研究参数。先确认最上面那 1 条今日练习,点蓝色按钮进入复盘。"
            href = "#today-practice"
            label = "去生成观察记录"
            steps = (
                ("看三件事", "观察材料、练习规模、为什么观察它。"),
                ("只点一条", "第一次只做最上面那条推荐练习。"),
                ("下一屏", "系统会带你保存三问复盘。"),
            )
        elif continue_task_html:
            tag = "ONLY STEP"
            title = "小白模式:现在只生成今日练习"
            text = "你已经有教练拆解。不要展开进阶设置,先点蓝色按钮生成 1 条今日练习。"
            href = "#continue-learning-task"
            label = "去一键生成今日练习"
            steps = (
                ("不用调参数", "第一次不用改模板、数量或候选数。"),
                ("不会成交", "只是保存一条待观察练习。"),
                ("会回这里", "生成后回学习页继续下一步。"),
            )
        elif has_tasks:
            tag = "CURRENT STEP"
            title = "小白模式:回到当前步骤"
            text = "你已经开始第一圈。先跟着页面上方的当前步骤走,不要开新题。"
            href = "#learning-loop"
            label = "查看当前进度"
            steps = (
                ("先别换题", "当前第一圈完成前,新题会分散注意力。"),
                ("看蓝色按钮", "每一步只需要找当前蓝色主按钮。"),
                ("完成标准", "看到 6/6 和一条复盘记录。"),
            )
        else:
            tag = "ZERO START"
            title = "小白模式:现在只点一个按钮"
            text = "不用先配置 DeepSeek key,不用写提示词,也不用理解所有菜单。先点“一键开始第一关”。"
            href = "#learn-presets"
            label = "去一键开始第一关"
            steps = (
                ("不用会术语", "第一关会从“量化是什么”开始。"),
                ("不用写问题", "系统已经准备好推荐目标。"),
                ("不会下单", "只会创建学习任务和 1 条今日练习,不会自动成交。"),
            )
        steps_html = "".join(
            f"<div><b>{escape(step_title)}</b><span>{escape(step_text)}</span></div>"
            for step_title, step_text in steps
        )
        return f"""
<section class="card beginner-focus" id="beginner-focus">
  <div class="beginner-focus-head">
    <div>
      <span class="tag">{escape(tag)}</span>
      <strong>{escape(title)}</strong>
      <p>{escape(text)}</p>
    </div>
    <a class="btn blue" href="{escape(href, quote=True)}">{escape(label)}</a>
  </div>
  <div class="beginner-focus-steps">{steps_html}</div>
</section>
"""

    def learning_demo_preset(self, query) -> dict:
        _, preset = self.learning_preset_by_index(query.get("preset", ["0"])[0] if query else "0")
        return preset

    def handle_learning_sample_task(self, user, form):
        if not self.require_user_write_limit(user, "learning_sample_task", 20, 3600, "/learn"):
            return
        idx, preset = self.learning_preset_by_index(form.get("preset"))
        template = services.normalize_learning_template(preset.get("template") or "reversal")
        try:
            task_id = services.create_learning_task(
                self.con,
                user["id"],
                preset["goal"],
                preset["difficulty"],
                template,
                self.sample_learning_coach_text(preset),
            )
        except ValueError as exc:
            self.redirect("/learn?err=" + quote(str(exc)))
            return
        self.audit(
            "learning.sample_task_create",
            user=user,
            target_type="learning_task",
            target_id=task_id,
            detail={"preset": idx, "template": template, "quick_start": form.get("quick_start", "")},
        )
        if form.get("quick_start") in TRUE_VALUES:
            try:
                created_signal_count = services.create_practice_signals_from_learning_task(
                    self.con,
                    user["id"],
                    task_id,
                    f"第一关 · {services.LEARNING_TEMPLATES[template]}",
                    template,
                    qty="100",
                    limit=1,
                    rationale_note="第一关一键开始:先用一条小数量观察材料跑通学习闭环,不要当成现实买卖建议。",
                )
            except ValueError as exc:
                self.redirect(f"/learn/tasks/{task_id}?msg=" + quote("示例教练任务已创建。下一步点击“一键生成今日练习”。") + "&err=" + quote(str(exc)))
                return
            self.audit(
                "learning.sample_task_quick_signal_saved",
                user=user,
                target_type="learning_task",
                target_id=task_id,
                detail={"preset": idx, "template": template, "count": created_signal_count},
            )
            if created_signal_count > 0:
                message = "第一关已准备好:示例教练拆解和 1 条今日练习都已生成。下一步只确认观察材料,再生成观察记录。"
                self.redirect("/learn?msg=" + quote(message) + "#today-practice")
                return
        summary = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM learning_tasks WHERE user_id=?) AS task_count,
                (SELECT COUNT(*) FROM learning_reflections WHERE user_id=?) AS reflection_count
            """,
            (int(user["id"]), int(user["id"])),
        ).fetchone()
        if int(summary["task_count"] or 0) > 1 and int(summary["reflection_count"] or 0) > 0:
            message = "下一关已创建:先看它和上一关有什么不同。"
        else:
            message = "示例教练任务已创建。下一步点击“一键生成今日练习”。"
        self.redirect(f"/learn/tasks/{task_id}?msg=" + quote(message))

    def handle_learning_next_task_quick_start(self, user, form):
        if not self.require_user_write_limit(user, "learning_next_task.quick_start", 10, 600, "/learn"):
            return
        idx, preset = self.learning_preset_by_index(form.get("preset") or "4")
        latest_reflection = self.con.execute(
            """
            SELECT t.id AS task_id
            FROM learning_reflections r
            JOIN practice_signals s ON s.id=r.practice_signal_id AND s.user_id=r.user_id
            JOIN learning_tasks t ON t.id=s.learning_task_id AND t.user_id=s.user_id
            WHERE r.user_id=?
            ORDER BY r.updated_at DESC, r.id DESC
            LIMIT 1
            """,
            (int(user["id"]),),
        ).fetchone()
        if latest_reflection is None:
            self.redirect("/learn?err=" + quote("先完成第一圈 6/6,再开始下一关。"))
            return
        latest_task_id = int(latest_reflection["task_id"] or 0)
        task = self.con.execute(
            """
            SELECT *
            FROM learning_tasks
            WHERE user_id=? AND id>?
            ORDER BY id
            LIMIT 1
            """,
            (int(user["id"]), latest_task_id),
        ).fetchone()
        created = False
        if task is None:
            template = services.normalize_learning_template(preset.get("template") or "momentum")
            try:
                task_id = services.create_learning_task(
                    self.con,
                    user["id"],
                    preset["goal"],
                    preset["difficulty"],
                    template,
                    self.sample_learning_coach_text(preset),
                )
            except ValueError as exc:
                self.redirect("/learn?err=" + quote(str(exc)))
                return
            task = services.learning_task(self.con, user["id"], task_id)
            created = True
        if task is None:
            self.redirect("/learn?err=" + quote("第二关任务创建失败,请稍后再试。"))
            return
        task_id = int(task["id"])
        sequence = int(
            self.con.execute(
                "SELECT COUNT(*) FROM learning_tasks WHERE user_id=? AND id<=?",
                (int(user["id"]), task_id),
            ).fetchone()[0]
        )
        task_template = services.normalize_learning_template(task["template"] or "reversal")
        stage_label = {1: "第一关", 2: "第二关", 3: "第三关", 4: "第四关"}.get(sequence, f"第 {sequence} 关")
        stage_ready_label = f"{stage_label}已准备好"
        if task_template == "risk_review":
            stage_ready_label = f"{stage_label}风险边界已准备好"
        state = self.con.execute(
            """
            SELECT
                COUNT(CASE WHEN s.status='pending' THEN 1 END) AS pending_count,
                COUNT(CASE WHEN s.status='executed' AND r.id IS NULL THEN 1 END) AS unreviewed_count,
                COUNT(r.id) AS reflection_count
            FROM practice_signals s
            LEFT JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=s.user_id
            WHERE s.user_id=? AND s.learning_task_id=?
            """,
            (int(user["id"]), task_id),
        ).fetchone()
        pending_count = int(state["pending_count"] or 0) if state else 0
        unreviewed_count = int(state["unreviewed_count"] or 0) if state else 0
        reflection_count = int(state["reflection_count"] or 0) if state else 0
        created_signal_count = 0
        if pending_count <= 0 and unreviewed_count <= 0 and reflection_count <= 0:
            template = self.learning_task_practice_template(task)
            rationale_note = (
                f"{stage_label}风险边界一键开始:先用一条小数量观察材料,写清数量边界、回撤边界和停止条件。"
                if task_template == "risk_review"
                else f"{stage_label}一键开始:用同样小数量换一个观察角度,和上一关做对照复盘。"
            )
            try:
                created_signal_count = services.create_practice_signals_from_learning_task(
                    self.con,
                    user["id"],
                    task_id,
                    f"{stage_label} · {services.LEARNING_TEMPLATES[task_template]}",
                    template,
                    qty="100",
                    limit=1,
                    rationale_note=rationale_note,
                )
            except Exception as exc:  # noqa: BLE001 - beginner-facing validation
                self.redirect(f"/learn/tasks/{task_id}?err=" + quote(str(exc)))
                return
            pending_count += created_signal_count
        if unreviewed_count > 0:
            target = "/learn#learning-review"
            message = f"{stage_label}已经生成观察记录。下一步只保存三问复盘。"
        elif reflection_count > 0:
            target = "/learn#learning-journey"
            message = f"{stage_label}已经完成。回到学习轨迹查看学习记录。"
        else:
            target = "/learn#today-practice"
            message = f"{stage_ready_label}:已经生成 1 条今日练习。下一步只确认观察材料,再生成观察记录。"
        self.audit(
            "learning.next_task_quick_start",
            user=user,
            target_type="learning_task",
            target_id=task_id,
            detail={"preset": idx, "created_task": created, "created_signals": created_signal_count, "pending": pending_count},
        )
        self.redirect(self.path_with_notice(target, "msg", message))

    def render_learning_demo(self, query, head: bool = False):
        preset = self.learning_demo_preset(query)
        template_label = services.LEARNING_TEMPLATES.get(preset["template"], str(preset["template"]))
        rows_html = "".join(
            "<tr>"
            f"<td data-label=\"练习对象\">{escape(row['name'])}</td>"
            f"<td data-label=\"模拟动作\">{escape(row['action'])}</td>"
            f"<td data-label=\"这一步学习什么\">{escape(row['why'])}</td>"
            "</tr>"
            for row in LEARNING_DEMO_ROWS
        )
        user = self.current_user()
        demo_reflection_count = 0
        if user:
            demo_reflection_count = int(
                self.con.execute(
                    "SELECT COUNT(*) FROM learning_reflections WHERE user_id=?",
                    (int(user["id"]),),
                ).fetchone()[0]
            )
        primary = (
            '<a class="btn blue" href="/learn">回到我的学习工作台</a>'
            if user
            else '<a class="btn blue" href="/register">注册后创建自己的练习</a>'
        )
        mobile_primary_href = "/learn" if user else "/register"
        mobile_primary_text = "回学习工作台" if user else "注册开始"
        mobile_primary_hint = "回到自己的学习工作台" if user else "注册后创建自己的练习"
        if not user:
            secondary = '<a class="btn secondary" href="/login">已有账号登录</a>'
        elif demo_reflection_count > 0:
            secondary = '<a class="btn secondary" href="/app">高级模拟盘</a>'
        else:
            secondary = '<span class="muted">完成 6/6 后再看高级模拟盘。</span>'
        if user and demo_reflection_count > 0:
            followup_title = "看完后可以继续第二关"
            followup_intro = "你已经完成过一次 6/6 闭环。现在可以回学习工作台换一个目标,也可以进入高级模拟盘查看更完整的账户和持仓细节。"
            followup_points = (
                ("回学习轨迹", "先看上一次复盘,再决定第二关要练什么。"),
                ("换一个目标", "继续用预设任务,不用一次学习所有策略。"),
                ("再看高级页", "高级模拟盘只用于深入查看模拟记录。"),
            )
            followup_actions = f'{primary} {secondary}'
        elif user:
            followup_title = "看完后只回学习工作台"
            followup_intro = "你还没完成第一次 6/6 闭环。现在不要配置 key、不要看榜单、不要进高级模拟盘,先回学习工作台点蓝色按钮。"
            followup_points = (
                ("第一屏看什么", "学习工作台会把下一步放在最上面。"),
                ("第一个动作", "点“一键开始第一关”或继续当前任务。"),
                ("暂时不用 key", "第一圈可以用示例教练完成,不调用 DeepSeek。"),
            )
            followup_actions = primary
        else:
            followup_title = "看完后只做下一步"
            followup_intro = "注册后不会把你丢进复杂模拟盘。第一屏是学习工作台,只需要点蓝色推荐按钮开始第一关。"
            followup_points = (
                ("注册后去哪里", "先进入学习工作台,不是高级模拟盘。"),
                ("第一下点什么", "点“一键开始第一关”,不用自己写提示词。"),
                ("不用先配置 AI key", "第一圈用示例教练,不调用 DeepSeek。"),
            )
            followup_actions = f'{primary} <a class="btn secondary" href="/login">已有账号登录</a>'
        followup_point_html = "".join(
            f"<div><b>{escape(title)}</b><p>{escape(text)}</p></div>"
            for title, text in followup_points
        )
        followup_html = f"""
<section class="card demo-next demo-start-next">
  <span class="tag">NEXT</span>
  <h2>{escape(followup_title)}</h2>
  <p>{escape(followup_intro)}</p>
  <div class="task-action-points">{followup_point_html}</div>
  <p>{followup_actions}</p>
</section>
"""
        mobile_demo_next_bar = f"""
<div class="mobile-next-spacer" aria-hidden="true"></div>
<div class="mobile-next-bar" role="navigation" aria-label="手机示例下一步提示">
  <div><span>看完示例后</span><b>{escape(mobile_primary_hint)}</b></div>
  <a class="btn blue" href="{escape(mobile_primary_href, quote=True)}">{escape(mobile_primary_text)}</a>
</div>
"""
        body = f"""
<section class="card demo-next">
  <span class="demo-pill">免登录</span><span class="demo-pill">免 DeepSeek key</span><span class="demo-pill">不产生真实交易</span>
  <h2>3 分钟体验一次 AI 量化学习闭环</h2>
  <p>这里用一个固定示例演示完整路径:先理解概念,再看教练拆解,然后看到模拟练习草稿,最后知道该怎么复盘。你不用先懂代码,也不用先配置 API key。</p>
  <p>{primary} <a class="btn secondary" href="/lessons">先看量化三大坑</a> {secondary}</p>
</section>
<section class="card demo-next">
  <h2>真实学习页会一路告诉你下一步</h2>
  <p>你不用自己记住完整流程。登录后,页面会按进度告诉你已经完成了什么、还差哪一步、现在该点哪个按钮。</p>
  <div class="task-action-points">
    <div><b>刚完成 3/6</b><p>你已经选了目标,也拿到了教练拆解;下一步只一键生成今日练习。</p></div>
    <div><b>刚完成 4/6</b><p>练习已经生成,但还没有生成观察记录;确认观察材料、练习规模和依据后再生成观察记录。</p></div>
    <div><b>刚完成 5/6</b><p>模拟观察记录已经生成;不用判断赚亏,先保存一条三问复盘。</p></div>
    <div><b>完成 6/6</b><p>学习成果已保存。第一圈可以停在这里,想巩固时再开第二关。</p></div>
  </div>
</section>
<section class="demo-loop">
  <div class="card">
    <h2>你选择的学习目标</h2>
    <p><span class="badge">{escape(preset["level"])}</span> <span class="badge">{escape(template_label)}</span></p>
    <h3>{escape(preset["title"])}</h3>
    <p>{escape(preset["goal"])}</p>
  </div>
  <div class="card">
    <h2>这次体验要完成什么</h2>
    <ol class="guide-list">
      <li>看懂“量化不是预测神话”。</li>
      <li>看懂系统如何把目标变成练习。</li>
      <li>知道练习完成后该问哪 3 个复盘问题。</li>
    </ol>
  </div>
</section>
<section class="card">
  <h2>示例教练会这样拆解</h2>
  <div class="markdown-body">{render_markdown(LEARNING_DEMO_COACH_MARKDOWN)}</div>
</section>
<section class="card">
  <h2>系统会生成这样的练习草稿</h2>
  <p class="msg">这只是示例,不会写入你的模拟盘,也不是买卖建议。真实登录后,你需要确认后才会保存练习,保存后也不会自动成交。</p>
  <table class="learning-mobile-table"><thead><tr><th>练习对象</th><th>模拟动作</th><th>这一步学习什么</th></tr></thead><tbody>{rows_html}</tbody></table>
</section>
<section class="card">
  <h2>第一次复盘只回答 3 个问题</h2>
  <div class="demo-checklist">
	    <div><strong>我这次想练什么?</strong><p>能不能用一句话说明为什么看这些对象。</p></div>
	    <div><strong>我有没有按小数量规则做?</strong><p>例如最多观察几个对象、每个对象多少模拟数量。</p></div>
	    <div><strong>下次先改哪一点?</strong><p>复盘重点不是短期赚亏,而是下次能不能把一个小动作做清楚。</p></div>
  </div>
</section>
{followup_html}
{mobile_demo_next_bar}
"""
        self.send_html(
            "学习体验",
            body,
            user=user,
            head=head,
            meta={
                "title": "3 分钟学习体验 · OurWorlds Quant",
                "description": "免登录、免 DeepSeek API key,先体验一次 AI 量化学习闭环:目标、教练拆解、模拟练习和复盘问题。",
                "url": f"{self.base_url()}/learn/demo",
            },
        )

    def learning_task_id_from_path(self, path: str) -> int:
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "learn" or parts[1] != "tasks":
            raise ValueError("学习任务路径无效")
        return int(parts[2])

    def learning_next_quest_form(self, user, ai_ready: bool, preset_idx: int, heading: str, summary: str) -> str:
        idx, preset = self.learning_preset_by_index(preset_idx)
        chips = (
            f'<span class="badge">{escape(preset["level"])}</span> '
            f'<span class="badge">{escape(services.LEARNING_TEMPLATES[preset["template"]])}</span>'
        )
        content = (
            f"{chips}"
            f"<strong>{escape(heading)}</strong>"
            f"<p>{escape(summary)}</p>"
            f"<small>{'让 AI 教练拆解' if ai_ready else '创建示例任务'}</small>"
        )
        if ai_ready:
            return (
                '<form class="quest-form" method="post" action="/learn/coach">'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="goal" value="{escape(preset["goal"], quote=True)}">'
                f'<input type="hidden" name="difficulty" value="{escape(preset["difficulty"])}">'
                f'<input type="hidden" name="template" value="{escape(preset["template"])}">'
                f'<button type="submit" class="quest-card">{content}</button>'
                "</form>"
            )
        return (
            '<form class="quest-form" method="post" action="/learn/sample-task">'
            f"{csrf_input(user)}"
            f'<input type="hidden" name="preset" value="{idx}">'
            f'<button type="submit" class="quest-card">{content}</button>'
            "</form>"
        )

    def learning_recommended_next_html(self, user, ai_ready: bool, current_task_id: int) -> str:
        later = self.con.execute(
            "SELECT id FROM learning_tasks WHERE user_id=? AND id>? ORDER BY id LIMIT 1",
            (int(user["id"]), int(current_task_id)),
        ).fetchone()
        if later is not None:
            return (
                '<details class="advanced-practice review-next-recommend">'
                "<summary>可选:下一关已经创建<span>第一圈已经完成;想继续时再打开。</span></summary>"
                '<div class="advanced-practice-body">'
                "<strong>下一关已经创建</strong>"
                "<p>你已经有后续学习任务,建议回到学习轨迹继续。</p>"
                '<a class="btn blue" href="#learning-journey">查看学习轨迹</a>'
                "</div></details>"
            )
        idx, preset = self.learning_preset_by_index(4)
        title = "可选第二关: 做一次动量观察"
        text = "今天第一圈已经达标,不用马上继续。想巩固时,再用另一种常见策略跑一遍闭环,比较它和这一关有什么不同。"
        if ai_ready:
            action = (
                '<form method="post" action="/learn/coach">'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="goal" value="{escape(preset["goal"], quote=True)}">'
                f'<input type="hidden" name="difficulty" value="{escape(preset["difficulty"])}">'
                f'<input type="hidden" name="template" value="{escape(preset["template"])}">'
                '<button class="secondary" type="submit">可选:创建第二关</button>'
                "</form>"
            )
        else:
            action = (
                '<form method="post" action="/learn/sample-task">'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="preset" value="{idx}">'
                '<button class="secondary" type="submit">可选:创建第二关</button>'
                "</form>"
            )
        return (
            '<details class="advanced-practice review-next-recommend">'
            "<summary>可选:展开第二关建议<span>今天可以先停在这里;想巩固时再打开。</span></summary>"
            '<div class="advanced-practice-body">'
            f"<strong>{escape(title)}</strong>"
            f"<p>{escape(text)}</p>"
            f"{action}"
            "</div></details>"
        )

    def learning_completion_next_html(
        self,
        user,
        ai_ready: bool,
        task_count: int,
        signal_count: int,
        reflection_count: int,
    ) -> str:
        latest_reflection = self.con.execute(
            """
            SELECT t.id AS task_id, t.template, r.hypothesis, r.execution_check, r.adjustment
            FROM learning_reflections r
            JOIN practice_signals s ON s.id=r.practice_signal_id AND s.user_id=r.user_id
            JOIN learning_tasks t ON t.id=s.learning_task_id AND t.user_id=s.user_id
            WHERE r.user_id=?
            ORDER BY r.updated_at DESC, r.id DESC
            LIMIT 1
            """,
            (int(user["id"]),),
        ).fetchone()
        latest_template = "上一关"
        latest_template_key = ""
        latest_task_id = 0
        latest_hypothesis = "把一个学习目标变成可以复盘的练习目标。"
        latest_execution = "按小数量模拟观察,没有把练习当成现实买卖建议。"
        latest_adjustment = "把上次想改的小动作带到下一关。"
        if latest_reflection is not None:
            latest_task_id = int(latest_reflection["task_id"] or 0)
            latest_template_key = str(latest_reflection["template"] or "")
            latest_template = services.LEARNING_TEMPLATES.get(latest_template_key, latest_template_key or "上一关")
            latest_hypothesis = str(latest_reflection["hypothesis"] or latest_hypothesis)
            latest_execution = str(latest_reflection["execution_check"] or latest_execution)
            latest_adjustment = str(latest_reflection["adjustment"] or latest_adjustment)
        if latest_template_key == "risk_review":
            primary_idx, primary_preset = self.learning_preset_by_index(7)
            mission_title = "下次回来只做 3 分钟模拟盘复盘"
            mission_text = "今天可以停。下次回来不要打开复杂报表,先把一条模拟记录拆成问题、依据和下次动作。"
            mission_button = "一键开始第四关并生成练习"
            mission_continue = "继续第四关并生成练习"
            mission_points = (
                ("回来先点哪里", "点上面的第四关按钮,系统继续用示例教练创建任务。"),
                ("这次只看什么", "只看一条模拟记录怎样变成复盘问题。"),
                ("继续给你什么反馈", "学习轨迹会多一条复盘方法记录。"),
            )
            details_summary = "可选:展开第四关建议"
            details_hint = "今天不用继续;想巩固时再打开。"
            optional_start_label = "可选:开始第四关"
            optional_continue_label = "可选:继续第四关"
            focus_title = "可选第四关: 学会复盘模拟盘"
            focus_text = "你已经练过观察和风险边界。下一关只看如何把模拟记录变成可改进的问题,不研究完整报表。"
            focus_points = (
                ("为什么是第四关?", "你已经会跑小闭环,现在练“怎么从记录里提问题”。"),
                ("这次重点看什么?", "依据、执行是否偏离、下次先改哪一个小动作。"),
                ("带着什么继续?", latest_execution),
            )
        elif reflection_count >= 2:
            primary_idx, primary_preset = self.learning_preset_by_index(6)
            mission_title = "下次回来只做 3 分钟风险边界"
            mission_text = "今天可以停。下次回来不要打开复杂指标,直接给一条小数量观察补上数量、回撤和停止条件。"
            mission_button = "一键开始第三关风险练习"
            mission_continue = "继续第三关并生成风险练习"
            mission_points = (
                ("回来先点哪里", "点上面的第三关按钮,系统会创建风险边界任务并生成 1 条练习。"),
                ("这次只写什么", "数量边界、回撤边界、停止条件,不要扩展到完整风控体系。"),
                ("继续给你什么反馈", "再完成一次 6/6 后,学习轨迹会多一条风险边界记录。"),
            )
            details_summary = "可选:展开第三关建议"
            details_hint = "今天不用继续;想巩固时再打开。"
            optional_start_label = "可选:开始第三关"
            optional_continue_label = "可选:继续第三关"
            focus_title = "可选第三关: 先补风险边界"
            focus_text = "你已经比较过两种观察角度。下一关不换成复杂交易,只给练习补上数量、回撤和停止条件。"
            focus_points = (
                ("为什么是第三关?", "先有观察,再有对照,现在才补风险边界。"),
                ("这次重点看什么?", "每个对象用多少、亏损扩大看什么、什么时候停止练习。"),
                ("带着什么继续?", latest_adjustment),
            )
        else:
            primary_idx, primary_preset = self.learning_preset_by_index(4)
            mission_title = "下次回来只做 3 分钟第二关"
            mission_text = "今天可以停。下次回来不要重新研究菜单,直接从这里开始一个小对照练习:同样的小数量,换一个观察角度。"
            mission_button = "一键开始第二关并生成练习"
            mission_continue = "继续第二关并生成练习"
            mission_points = (
                ("回来先点哪里", "点上面的第二关按钮,系统继续用示例教练创建任务。"),
                ("这次只比什么", "比较上一关和下一关的观察角度、数量边界和复盘修正。"),
                ("继续给你什么反馈", "再完成一次 6/6 后,学习轨迹会多一条对照记录。"),
            )
            details_summary = "可选:展开第二关建议"
            details_hint = "今天不用继续;想巩固时再打开。"
            optional_start_label = "可选:开始第二关"
            optional_continue_label = "可选:继续第二关"
            focus_title = "可选第二关: 换一种策略练习"
            focus_text = "推荐你先做“动量观察”。第一关看反转,第二关看动量,这样不是重复点击,而是在比较两种常见想法哪里不同。"
            focus_points = (
                ("为什么是第二关?", "你已经会跑流程,现在要练“对照”:同样的数量、不同的观察角度。"),
                ("这次重点看什么?", "候选怎么来、是不是容易追涨、复盘时和第一关哪里不同。"),
                ("带着什么继续?", latest_execution),
            )
        later_task = None
        if latest_task_id > 0:
            later_task = self.con.execute(
                "SELECT id FROM learning_tasks WHERE user_id=? AND id>? ORDER BY id LIMIT 1",
                (int(user["id"]), latest_task_id),
            ).fetchone()
        if later_task is not None:
            primary_action = f'<a class="btn blue" href="/learn/tasks/{int(later_task["id"])}">{escape(optional_continue_label)}</a>'
        elif ai_ready:
            primary_action = (
                '<form method="post" action="/learn/coach">'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="goal" value="{escape(primary_preset["goal"], quote=True)}">'
                f'<input type="hidden" name="difficulty" value="{escape(primary_preset["difficulty"])}">'
                f'<input type="hidden" name="template" value="{escape(primary_preset["template"])}">'
                f'<button class="blue" type="submit">{escape(optional_start_label)}</button>'
                "</form>"
            )
        else:
            primary_action = (
                '<form method="post" action="/learn/sample-task">'
                f"{csrf_input(user)}"
                f'<input type="hidden" name="preset" value="{primary_idx}">'
                f'<button class="blue" type="submit">{escape(optional_start_label)}</button>'
                "</form>"
            )
        return_action_label = mission_continue if later_task is not None else mission_button
        return_action = (
            '<form method="post" action="/learn/next-task/quick-start">'
            f"{csrf_input(user)}"
            f'<input type="hidden" name="preset" value="{primary_idx}">'
            f'<button class="blue" type="submit">{escape(return_action_label)}</button>'
            "</form>"
        )
        quests = (
            (5, "备选: 理解模型预测候选", "把模型预测当成学习材料,练习不盲信 AI 输出。"),
            (6, "第三关: 先补风险边界", "学习仓位、回撤、成本和停止条件,避免只盯着涨跌。"),
            (7, "第四关: 学会复盘模拟盘", "把成交、持仓和依据转成问题,积累自己的学习记录。"),
        )
        quest_html = "".join(self.learning_next_quest_form(user, ai_ready, *quest) for quest in quests)
        mode_text = (
            "下方 3 分钟下一关会继续用示例教练,不调用 DeepSeek;展开完整关卡或自定义目标时才会用你配置的 AI 教练。"
            if ai_ready
            else "点击下一关会继续创建内置示例任务,不需要 DeepSeek key。"
        )
        mission_point_html = "".join(
            f"<div><b>{escape(point_title)}</b><p>{escape(point_text)}</p></div>"
            for point_title, point_text in mission_points
        )
        focus_point_html = "".join(
            f"<div><b>{escape(point_title)}</b><p>{escape(point_text)}</p></div>"
            for point_title, point_text in focus_points
        )
        return f"""
<div class="loop-complete">
	  <div class="loop-complete-head">
	    <div>
	      <span class="tag">FIRST LOOP COMPLETE</span>
	      <strong>第一次学习闭环完成</strong>
	      <p>你已经留下了第一条可复盘的学习记录。第一圈已经达标,现在可以先停在这里;想继续时,再换一个角度练第二关。</p>
	      <p class="muted">{escape(mode_text)}</p>
    </div>
    <div class="achievement-metrics">
      <div><b>{task_count}</b><span>学习任务</span></div>
      <div><b>{signal_count}</b><span>模拟练习</span></div>
      <div><b>{reflection_count}</b><span>复盘记录</span></div>
	    </div>
	  </div>
		  <div class="first-win">
		    <strong>你刚解锁的不是收益,而是一种学习能力</strong>
		    <p>小白最重要的第一步不是猜对涨跌,而是把一个模糊问题变成能记录、能观察、能修正的练习。你已经完成了这件事。</p>
		    <div class="first-win-grid">
		      <div><b>会提问题</b><p>{escape(latest_template)}不是买卖指令,而是一个观察角度。</p></div>
		      <div><b>会留证据</b><p>{escape(latest_hypothesis)}</p></div>
		      <div><b>会做修正</b><p>{escape(latest_adjustment)}</p></div>
		    </div>
		  </div>
		  <div class="achievement-badge">
		    <div class="achievement-badge-mark">1</div>
		    <div>
		      <b>第一枚学习徽章:把想法变成可复盘练习</b>
		      <span>这枚徽章代表你完成了“提出目标、生成练习、生成观察记录、保存复盘”的最小闭环。以后每一关都在重复并改进这个动作。</span>
		    </div>
		  </div>
		  <div class="review-done-note">
	    <strong>现在可以停在这里</strong>
	    <p>第一次闭环已经完成。你可以先关掉页面,之后回来会在学习轨迹里看到这条记录;也可以继续下面的可选第二关。</p>
	    <div class="review-done-actions">
	      <a class="btn blue" href="#learning-journey">查看我的学习轨迹</a>
	      <a class="btn secondary" href="/app">稍后再看高级模拟盘</a>
	    </div>
	  </div>
	  <div class="return-mission" id="next-visit-mission">
	    <div class="return-mission-head">
	      <div>
	        <span class="tag">NEXT VISIT</span>
	        <strong>{escape(mission_title)}</strong>
	        <p>{escape(mission_text)}</p>
	      </div>
	      <div class="return-mission-action">
	        {return_action}
	        <p class="muted">可选任务,不影响今天已经完成的 6/6。</p>
	      </div>
	    </div>
	    <div class="return-mission-grid">{mission_point_html}</div>
	  </div>
	  <details class="advanced-practice">
	    <summary>{escape(details_summary)}<span>{escape(details_hint)}</span></summary>
	    <div class="advanced-practice-body">
	      <div class="next-focus">
	        <div class="next-focus-head">
	          <div>
	            <span class="tag">OPTIONAL NEXT</span>
	            <strong>{escape(focus_title)}</strong>
	            <p>{escape(focus_text)}</p>
	          </div>
	          {primary_action}
	        </div>
	        <div class="next-focus-points">{focus_point_html}</div>
	      </div>
	      <div class="bridge-compare">
	        <h3>第二关怎么比较?</h3>
	        <div class="bridge-compare-grid">
	          <div><b>上一关是什么?</b><p>{escape(latest_template)}。先记住它的观察角度,不要只看涨跌。</p></div>
	          <div><b>下一关比什么?</b><p>候选怎么来、数量边界是否一样、复盘时哪里需要修正。</p></div>
	          <div><b>带走一句话</b><p>{escape(latest_adjustment)}</p></div>
	        </div>
	      </div>
	      <div class="review-done-note">
	        <strong>你已经掌握的 3 个动作</strong>
	        <p>下一关不是从零开始,而是重复这三个动作,每次只把一个地方做得更清楚。</p>
	        <div class="review-done-list">
	          <div><b>把问题变成目标</b><p>不问“买什么”,先写清楚这次想学什么、验证什么。</p></div>
		          <div><b>把目标变成模拟练习</b><p>系统先生成待观察计划,你确认后才生成观察记录。</p></div>
		          <div><b>把结果变成复盘</b><p>用想练什么、有没有按规则做、下次改哪一点留下自己的学习记录。</p></div>
	        </div>
	      </div>
	      <h3>更多下一关推荐</h3>
	      <div class="next-quests">{quest_html}</div>
	    </div>
	  </details>
	</div>
"""

    def learning_loop_progress_html(self, user, ai_ready: bool) -> str:
        row = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM learning_tasks WHERE user_id=?) AS task_count,
                (SELECT id FROM learning_tasks WHERE user_id=? ORDER BY id DESC LIMIT 1) AS latest_task_id,
                (SELECT COUNT(*) FROM practice_signals WHERE user_id=? AND learning_task_id IS NOT NULL AND status IN ('pending','executed')) AS signal_count,
                (SELECT COUNT(*) FROM practice_signals WHERE user_id=? AND learning_task_id IS NOT NULL AND status='pending') AS pending_count,
                (SELECT COUNT(*) FROM practice_signals WHERE user_id=? AND learning_task_id IS NOT NULL AND status='executed') AS executed_count,
                (SELECT COUNT(*) FROM learning_reflections WHERE user_id=?) AS reflection_count
            """,
            (
                int(user["id"]),
                int(user["id"]),
                int(user["id"]),
                int(user["id"]),
                int(user["id"]),
                int(user["id"]),
            ),
        ).fetchone()
        task_count = int(row["task_count"] or 0)
        latest_task_id = int(row["latest_task_id"] or 0)
        signal_count = int(row["signal_count"] or 0)
        pending_count = int(row["pending_count"] or 0)
        executed_count = int(row["executed_count"] or 0)
        reflection_count = int(row["reflection_count"] or 0)
        done = {
            "concept": task_count > 0,
            "goal": task_count > 0,
            "coach": task_count > 0,
            "practice": signal_count > 0,
            "observe": executed_count > 0,
            "reflection": reflection_count > 0,
        }
        steps = [
            ("concept", "1", "理解概念", "先知道量化是可记录、可复盘的学习流程。"),
            ("goal", "2", "选择目标", "从预设问题或自己的问题开始。"),
            ("coach", "3", "看拆解", "看 AI 或示例教练如何拆成任务。"),
            ("practice", "4", "生成练习", "把目标变成今日练习草稿。"),
            ("observe", "5", "生成观察记录", "确认后生成一条模拟观察记录。"),
            ("reflection", "6", "保存复盘", "留下自己的三问学习记录。"),
        ]
        completed = sum(1 for key, *_ in steps if done[key])
        remaining = max(0, len(steps) - completed)
        if completed <= 0:
            time_text = "预计 3-5 分钟跑完第一次闭环"
        elif remaining > 0:
            time_text = f"还剩 {remaining} 步,先做当前这一步"
        else:
            time_text = "第一次闭环已完成"
        current_key = next((key for key, *_ in steps if not done[key]), "reflection")
        if completed >= len(steps):
            next_title = "第一次闭环完成"
            next_text = "你已经完成一次从概念、目标、拆解、练习到复盘的完整学习闭环。下一步可以换一个预设任务再练一次。"
            next_href = "#learn-presets"
            next_label = "继续选择新任务"
        elif task_count == 0:
            next_title = "下一步:先记住一句话,再点按钮"
            next_text = "量化学习不是让 AI 告诉你买什么,而是把一个想法变成规则、模拟练习和复盘记录;不知道问什么也没关系,回到上方点蓝色推荐按钮。"
            next_href = "#learn-presets"
            next_label = "看完,点蓝色推荐按钮"
        elif signal_count == 0:
            next_title = "下一步:生成一个练习"
            next_text = "打开最近的学习任务,先点击“一键生成今日练习”;想调参数时再展开进阶草稿设置。"
            next_href = f"/learn/tasks/{latest_task_id}" if latest_task_id else "#learn-presets"
            next_label = "打开最近学习任务"
        elif pending_count > 0 and executed_count == 0:
            next_title = "下一步:生成观察记录"
            next_text = "到今日练习里确认观察材料、练习规模和依据,再点击生成观察记录;系统只会生成一条模拟观察记录。"
            next_href = "#today-practice"
            next_label = "去今日练习"
        elif reflection_count == 0:
            next_title = "下一步:保存第一次复盘"
            next_text = "先不用判断赚亏,点击“一键完成 6/6”也可以;完成后再慢慢改成自己的三句话。"
            next_href = "#learning-review"
            next_label = "去完成 6/6"
        else:
            next_title = "下一步:继续练习"
            next_text = "你已经保存过复盘,可以继续选择新目标,逐步积累自己的观察记录。"
            next_href = "#learn-presets"
            next_label = "继续选择新任务"
        cards = []
        for key, number, title, desc in steps:
            klass = "done" if done[key] else "current" if key == current_key else "todo"
            status = "DONE" if done[key] else "NOW" if key == current_key else "TODO"
            cards.append(
                f'<div class="loop-step {klass}">'
                f"<span>{status} · {number}/6</span>"
                f"<strong>{escape(title)}</strong>"
                f"<p>{escape(desc)}</p>"
                "</div>"
            )
        if completed >= len(steps):
            next_panel = self.learning_completion_next_html(user, ai_ready, task_count, signal_count, reflection_count)
        else:
            primer_html = (
                """
  <div class="task-action-points">
    <div><b>量化投资是什么</b><p>把投资想法写成规则,再用数据和模拟结果检查它。</p></div>
    <div><b>AI 在这里做什么</b><p>帮你解释、拆解和复盘,不替你预测涨跌或决定买卖。</p></div>
    <div><b>第一次只做什么</b><p>选一个问题,生成今日练习,再生成观察记录并保存三问复盘。</p></div>
  </div>
"""
                if task_count == 0
                else ""
            )
            next_panel = f"""
  <div class="loop-next">
    <p><strong>{escape(next_title)}</strong><br>{escape(next_text)}</p>
    <a class="btn blue" href="{escape(next_href, quote=True)}">{escape(next_label)}</a>
  </div>
  {primer_html}
"""
        return f"""
<section class="card loop-progress" id="learning-loop">
  <div class="loop-progress-head">
    <div>
      <h2>第一次学习闭环</h2>
      <p>目标是在 3-5 分钟内完成一次正反馈:看懂一个概念,生成一次模拟练习,并保存一条自己的复盘。</p>
    </div>
    <div class="loop-progress-score">已完成 {completed}/6<span>{escape(time_text)}</span></div>
  </div>
  <div class="loop-steps">{''.join(cards)}</div>
  {next_panel}
</section>
"""

    def learning_today_practice_html(self, user) -> str:
        rows = self.con.execute(
            """
            SELECT
                s.id,
                s.code,
                s.side,
                s.qty,
                s.rationale,
                s.learning_task_id,
                m.name,
                m.price,
                t.goal,
                t.template
            FROM practice_signals s
            JOIN learning_tasks t ON t.id=s.learning_task_id AND t.user_id=s.user_id
            LEFT JOIN market_prices m ON m.code=s.code
            WHERE s.user_id=? AND s.status='pending' AND s.learning_task_id IS NOT NULL
            ORDER BY s.id DESC
            LIMIT 6
            """,
            (int(user["id"]),),
        ).fetchall()
        if not rows:
            return ""
        cards = []
        for idx, row in enumerate(rows):
            signal_id = int(row["id"])
            title = row["name"] or row["code"]
            template = services.LEARNING_TEMPLATES.get(str(row["template"] or ""), str(row["template"] or "学习练习"))
            price_text = money(row["price"]) if row["price"] is not None else "暂无价格"
            rationale = learning_display_rationale(row["rationale"])
            observation_text = learning_observation_action(row["side"], int(row["qty"]))
            first_badge = "推荐先做这一条" if idx == 0 else "备用练习"
            card_hint = (
                "第一次只点这一条的蓝色按钮;回看和取消入口都收在可选区。"
                if idx == 0
                else "这是备用练习。第一次闭环可以先留着不动,等熟悉后再回来。"
            )
            card_id = ' id="first-practice-card"' if idx == 0 else ""
            cards.append(
                f'<article class="practice-card"{card_id}>'
                f'<p><span class="badge">{escape(first_badge)}</span> <span class="badge">还没开始</span> <span class="badge">{escape(template)}</span></p>'
                '<h3>确认这 1 条模拟练习</h3>'
                f'<div class="practice-focus"><b>一句话任务</b><p>把 {escape(title)} 当作练习材料,练习“看依据 -> 生成观察记录 -> 三问复盘”,不是判断它会不会涨。</p></div>'
                '<div class="practice-summary">'
                f'<div><span>观察材料</span><strong>{escape(title)}</strong><small>先当作练习样本</small></div>'
                f'<div><span>练习规模</span><strong>{escape(observation_text)}</strong><small>小数量,只为训练流程</small></div>'
                f'<div><span>学习重点</span><strong>{escape(template)}</strong><small>看依据,不是看涨跌</small></div>'
                '<div><span>下一步</span><strong>进入三问复盘</strong><small>点蓝色按钮后出现</small></div>'
                "</div>"
                f'<p class="practice-rationale"><b>我为什么观察它?</b>{escape(rationale)}</p>'
                '<div class="practice-ready" aria-label="点按钮前只核对三件事">'
                '<b>点前只核对 3 件事</b>'
                '<span><strong>材料</strong><small>知道这是练习样本。</small></span>'
                '<span><strong>规模</strong><small>确认数量很小。</small></span>'
                '<span><strong>依据</strong><small>能读懂一句理由。</small></span>'
                '</div>'
                f'<p class="practice-next"><b>第一次怎么选?</b>{escape(card_hint)}</p>'
                '<div class="actions practice-primary-action">'
                f'<form method="post" action="/practice-signals/{signal_id}/execute">'
                f'{csrf_input(user)}<input type="hidden" name="next" value="/learn#learning-review">'
                '<button class="blue" type="submit">生成模拟观察记录,去复盘</button>'
                '<small class="practice-action-note">只生成模拟学习记录,不是现实交易。</small></form>'
                "</div>"
                '<p class="practice-next"><b>点完会发生什么?</b>系统只会生成一条模拟观察记录,然后带你回答三问复盘;这不是现实交易。</p>'
                '<details class="practice-detail">'
                '<summary>可选:查看代码和模拟价<span>第一次可以先不展开;这些只用于生成模拟记录。</span></summary>'
                '<div class="practice-detail-grid">'
                f'<div><b>代码</b><span>{escape(row["code"])}</span></div>'
                f'<div><b>模拟记录参考价</b><span>{price_text}</span></div>'
                "</div></details>"
                '<ul class="practice-checklist">'
                '<li><b>确认 1</b>我能看懂“为什么观察它”的一句话。</li>'
                '<li><b>确认 2</b>数量很小,只用于训练流程,不是现实委托。</li>'
                '<li><b>确认 3</b>点蓝色按钮后会自动跳到三问复盘。</li>'
                "</ul>"
                '<details class="practice-detail practice-optional-actions">'
                '<summary>可选:回看教练或管理这条练习<span>第一次可以先不展开;这里只有回看和暂不练这条。</span></summary>'
                '<div class="practice-detail-grid">'
                f'<div><b>教练拆解</b><span><a href="/learn/tasks/{int(row["learning_task_id"])}">回看这一关</a></span></div>'
                '<div><b>暂不练这条</b>'
                f'<form method="post" action="/practice-signals/{signal_id}/cancel">'
                f'{csrf_input(user)}<input type="hidden" name="next" value="/learn#today-practice">'
                '<button class="secondary" type="submit">暂不练这条</button></form>'
                "</div>"
                "</div></details>"
                "</article>"
            )
        return f"""
<section class="card demo-next" id="today-practice">
  <div class="task-action-head">
    <div>
      <span class="tag">READY</span>
      <strong>刚完成 4/6:确认练习后生成观察记录</strong>
      <p>今日练习已经生成,但还没有生成观察记录。现在只看观察材料、练习规模和依据,看懂后点蓝色按钮“生成模拟观察记录,去复盘”。</p>
    </div>
  </div>
  <div class="task-action-points">
    <div><b>已经完成</b><p>学习目标和教练拆解已经变成今日练习。</p></div>
    <div><b>还没发生</b><p>系统还没有生成模拟观察记录,也不会产生现实交易。</p></div>
    <div><b>下一步</b><p>确认这 1 条练习,再进入三问复盘。</p></div>
  </div>
  {self.learning_promise_strip_html((
    ("预计 60 秒", "只确认 1 条最上面的练习。"),
    ("看三件事", "观察材料、练习规模、为什么观察它。"),
    ("不会真实交易", "只生成模拟观察记录,不是现实委托。"),
    ("点完去哪", "自动跳到三问复盘,完成最后一步。"),
  ))}
  <h2>今日练习</h2>
  <p>这些练习来自你的学习任务。第一次只看三件事:观察材料是什么、练习规模有多小、依据能不能读懂;代码和价格已经收进可选详情,先不用展开。确认后点蓝色按钮,系统只会生成一条模拟观察记录,不会产生现实交易。</p>
  <p class="practice-next"><b>第一次只建议生成 1 条观察记录。</b>如果页面里有多条练习,先选最上面一条点蓝色按钮;其他先留着不动。</p>
  <div class="practice-cards">{''.join(cards)}</div>
  <p class="muted">生成后,学习页会出现“观察复盘”卡。先回答三问并保存复盘,看到 6/6 后再看高级页面。</p>
</section>
"""

    def learning_continue_task_html(self, user) -> str:
        row = self.con.execute(
            """
            SELECT
                t.id,
                t.goal,
                t.template,
                t.difficulty,
                COUNT(DISTINCT CASE WHEN s.status IN ('pending','executed') THEN s.id END) AS signal_count,
                (SELECT COUNT(*) FROM learning_tasks p WHERE p.user_id=t.user_id AND p.id<=t.id) AS sequence,
                (SELECT COUNT(*) FROM learning_reflections r WHERE r.user_id=t.user_id) AS reflection_count
            FROM learning_tasks t
            LEFT JOIN practice_signals s ON s.learning_task_id=t.id AND s.user_id=t.user_id
            WHERE t.user_id=?
            GROUP BY t.id
            ORDER BY t.id DESC
            LIMIT 1
            """,
            (int(user["id"]),),
        ).fetchone()
        if row is None or int(row["signal_count"] or 0) > 0:
            return ""
        task_id = int(row["id"])
        template = services.LEARNING_TEMPLATES.get(str(row["template"] or ""), str(row["template"] or "学习练习"))
        difficulty = services.LEARNING_DIFFICULTIES.get(str(row["difficulty"] or ""), str(row["difficulty"] or "新手"))
        sequence = int(row["sequence"] or 1)
        reflection_count = int(row["reflection_count"] or 0)
        returning = reflection_count > 0 and sequence > 1
        goal_text = str(row["goal"] or "").strip()
        if len(goal_text) > 96:
            goal_text = goal_text[:96].rstrip() + "..."
        tag = "RETURN" if returning else "CONTINUE"
        title = f"回访继续:第 {sequence} 关待生成练习" if returning else "继续当前学习任务"
        intro = (
            f"你上次已经完成 {reflection_count} 条复盘记录。现在不用重新找入口,直接把第 {sequence} 关生成今日练习。"
            if returning
            else "你已经有教练拆解。下一步不用改参数,直接一键生成今日练习。"
        )
        location_text = (
            f"第 {sequence} 关还没生成练习;当前目标: {goal_text}"
            if returning
            else goal_text
        )
        cta_note = (
            f"点蓝色按钮会把第 {sequence} 关变成 1 条待观察练习,然后回到学习工作台继续,不会自动成交。"
            if returning
            else "点蓝色按钮只会生成待观察练习,然后回到学习工作台继续;不会自动成交。"
        )
        return f"""
<section class="card task-action-card" id="continue-learning-task">
  <div class="task-action-head">
    <div>
      <span class="tag">{escape(tag)}</span>
      <strong>{escape(title)}</strong>
      <p>{escape(intro)}</p>
    </div>
    <div class="next-action-cta">
      <form method="post" action="/learn/tasks/{task_id}/quick-save">
        {csrf_input(user)}
        <button class="blue" type="submit">一键生成今日练习</button>
      </form>
      <a class="btn secondary" href="/learn/tasks/{task_id}">回看教练拆解</a>
      <p class="muted">{escape(cta_note)}</p>
    </div>
  </div>
  {self.learning_promise_strip_html((
    ("预计 30 秒", "只点一次蓝色按钮,不用改参数。"),
    ("点完去哪", "自动回到学习工作台的今日练习。"),
    ("不会成交", "这里只保存待观察练习,不是现实交易。"),
    ("下一步", "看懂观察材料后再生成观察记录。"),
  ))}
  <div class="task-loop-hint resume-checkpoint">
    <b>回访定位:已完成 3/6</b>
    <span>概念、目标和教练拆解已经完成。现在只差生成今日练习、生成观察记录、保存复盘;先点上面的蓝色按钮。</span>
  </div>
  <div class="task-action-points">
    <div><b>已经完成 3/6</b><p>{escape(location_text)}</p></div>
    <div><b>练习类型</b><p>{escape(difficulty)} · {escape(template)}</p></div>
    <div><b>点完去哪</b><p>系统会回到学习工作台,你再确认观察材料并生成观察记录。</p></div>
  </div>
</section>
"""

    def learning_previous_reflection_row(self, user, current_task_id: int):
        return self.con.execute(
            """
            SELECT
                t.id AS task_id,
                t.template,
                r.hypothesis,
                r.execution_check,
                r.adjustment
            FROM learning_tasks t
            JOIN practice_signals s ON s.learning_task_id=t.id AND s.user_id=t.user_id
            JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=t.user_id
            WHERE t.user_id=? AND t.id<?
            ORDER BY t.id DESC, r.updated_at DESC
            LIMIT 1
            """,
            (int(user["id"]), int(current_task_id)),
        ).fetchone()

    def learning_recent_review_html(self, user) -> str:
        rows = self.con.execute(
            """
            SELECT
                s.id,
                s.code,
                s.side,
                s.qty,
                s.rationale,
                s.strategy_name,
                s.learning_task_id,
                s.updated_at,
                m.name,
                m.price AS current_price,
                t.goal,
                t.template,
                o.price AS executed_price,
                o.fee,
                o.amount,
                o.created_at AS executed_at
            FROM practice_signals s
            JOIN learning_tasks t ON t.id=s.learning_task_id AND t.user_id=s.user_id
            LEFT JOIN market_prices m ON m.code=s.code
            LEFT JOIN orders o ON o.id=s.order_id
            WHERE s.user_id=? AND s.status='executed' AND s.learning_task_id IS NOT NULL
            ORDER BY COALESCE(o.id, s.id) DESC
            LIMIT 3
            """,
            (int(user["id"]),),
        ).fetchall()
        if not rows:
            return ""
        key_row = ai_service.get_key_row(self.con, user["id"])
        ai_ready = key_row is not None and bool(int(key_row["enabled"]))
        ai_review_label = "可选:让 AI 教练复盘" if ai_ready else "可选:配置 AI 教练复盘"
        reflections = services.learning_reflections_for_signals(self.con, user["id"], [int(row["id"]) for row in rows])
        first_unreviewed = next((row for row in rows if int(row["id"]) not in reflections), None)
        focus_html = ""
        ready_html = ""
        if first_unreviewed is None:
            section_title = "学习成果"
            section_intro = "复盘已经保存。你已经完成 6/6,第一圈可以停在这里;下次回来先看这条记录,再决定是否继续第二关。"
        else:
            section_title = "观察复盘"
            section_intro = "你已经生成了一条模拟观察记录。第一次复盘不用判断涨跌,也不需要 AI key;先留下“我想练什么、我有没有按小数量规则做、下次先改哪一点”三句话就算完成。"
        if first_unreviewed is not None:
            focus_title = first_unreviewed["name"] or first_unreviewed["code"]
            ready_html = """
  <div class="task-action-head">
    <div>
      <span class="tag">FINAL STEP</span>
      <strong>刚完成 5/6:最后一步保存复盘</strong>
      <p>模拟观察记录已经生成。现在不用判断赚亏,也不用配置 AI key;先点下面的“一键完成 6/6”按钮,第一次学习闭环就完成。</p>
    </div>
  </div>
  <div class="task-action-points">
    <div><b>已经完成</b><p>目标、教练拆解、今日练习和观察记录已经串起来。</p></div>
    <div><b>还差一步</b><p>保存“想练什么、有没有按规则做、下次改哪里”三句话,就解锁 6/6。</p></div>
    <div><b>最省心做法</b><p>不会写时先用示例复盘,之后还能改成自己的话。</p></div>
  </div>
  {self.learning_promise_strip_html((
    ("预计 30 秒", "直接点一键完成 6/6 也可以。"),
    ("不看涨跌", "第一次只练复盘动作,不是判断输赢。"),
    ("完成标志", "页面出现 6/6 和第一枚学习徽章。"),
    ("可以停下", "第一圈达标后,今天不用马上继续。"),
  ))}
"""
            focus_previous = self.learning_previous_reflection_row(user, int(first_unreviewed["learning_task_id"]))
            if focus_previous is not None:
                previous_template = services.LEARNING_TEMPLATES.get(str(focus_previous["template"] or ""), str(focus_previous["template"] or "上一关"))
                current_template = services.LEARNING_TEMPLATES.get(str(first_unreviewed["template"] or ""), str(first_unreviewed["template"] or "这一关"))
                previous_adjustment = " ".join(str(focus_previous["adjustment"] or "上一关还没有写修正点。").split())[:120]
                focus_html = f"""
  <div class="review-focus">
	    <div class="review-focus-head review-first-action-head">
	      <div>
	        <span class="tag">COMPARE STEP</span>
	        <strong>第二关对照复盘:先比较,再保存</strong>
	        <p>你已经开始观察 {escape(focus_title)}。这次不要重新从零写,只比较上一关 {escape(previous_template)} 和这一关 {escape(current_template)} 哪里不同。</p>
      </div>
      <div class="review-primary-cta">
        <form method="post" action="/learn/reflections/quick-save">
          {csrf_input(user)}
          <input type="hidden" name="practice_signal_id" value="{int(first_unreviewed['id'])}">
          <button class="blue" type="submit">一键保存对照复盘</button>
        </form>
	        <p class="muted">点这个按钮会保存示例复盘,不调用 AI,不会产生现实交易。</p>
	      </div>
	    </div>
	    <div class="review-unlock" aria-label="保存对照复盘后会看到什么">
	      <div><b>点完会看到</b><p>这一关变成已复盘,学习轨迹会多一条对照记录。</p></div>
	      <div><b>不用判断涨跌</b><p>只比较观察角度、数量边界和上次想改的小动作。</p></div>
	      <div><b>以后还能改</b><p>示例复盘只是先占位,之后可以展开改成自己的话。</p></div>
	    </div>
	    <div class="review-template-note"><b>系统会先保存这三句话</b><p>上一关看什么、这一关看什么、上次想改的小动作这次有没有用上。</p></div>
	    <div class="review-focus-answers">
	      <div><b>上一关</b><p>{escape(previous_template)}: 先记住原来的观察角度。</p></div>
	      <div><b>这一关</b><p>{escape(current_template)}: 看候选来源和风险边界是否不同。</p></div>
	      <div><b>沿用修正</b><p>{escape(previous_adjustment)}</p></div>
    </div>
  </div>
"""
            else:
                focus_html = f"""
  <div class="review-focus">
	    <div class="review-focus-head review-first-action-head">
	      <div>
	        <span class="tag">FINAL STEP</span>
	        <strong>最后 30 秒:点一下完成 6/6</strong>
	        <p>你已经开始观察 {escape(focus_title)}。现在不用写专业分析,先点“一键完成 6/6 并保存示例复盘”留下示例记录,第一次闭环就完成了;以后可以随时改成自己的话。</p>
      </div>
      <div class="review-primary-cta">
        <form method="post" action="/learn/reflections/quick-save">
          {csrf_input(user)}
          <input type="hidden" name="practice_signal_id" value="{int(first_unreviewed['id'])}">
          <button class="blue" type="submit">一键完成 6/6 并保存示例复盘</button>
        </form>
	        <p class="muted">点这个按钮就完成 6/6,不调用 AI,以后还能修改。</p>
	      </div>
	    </div>
	    <div class="review-unlock" aria-label="保存复盘后会看到什么">
	      <div><b>点完马上看到</b><p>已完成 6/6、第一枚学习徽章和学习轨迹。</p></div>
	      <div><b>不用会分析</b><p>示例复盘会先帮你留下三句大白话。</p></div>
	      <div><b>今天可停</b><p>看到 6/6 后,第一圈已经达标,不必马上进高级模拟盘。</p></div>
	    </div>
	    <div class="review-template-note"><b>系统会先保存这三句话</b><p>我想练什么、有没有按小数量规则做、下次先改哪一点;之后可以改成自己的话。</p></div>
	    <div class="review-focus-answers">
	      <div><b>我想练什么</b><p>我观察它是为了学习一个想法怎样被记录。</p></div>
	      <div><b>我有没有按规则做</b><p>我只用小数量做模拟观察,不当作现实买卖建议。</p></div>
      <div><b>下次先改哪一点</b><p>下一次先比较依据、数量和风险边界。</p></div>
    </div>
  </div>
"""
        cards = []
        for row in rows:
            signal_id = int(row["id"])
            title = row["name"] or row["code"]
            template = services.LEARNING_TEMPLATES.get(str(row["template"] or ""), str(row["template"] or "学习练习"))
            executed_price = money(row["executed_price"]) if row["executed_price"] is not None else "暂无"
            observation_text = learning_observation_action(row["side"], int(row["qty"]))
            reflection = reflections.get(signal_id)
            hypothesis = str(reflection["hypothesis"] or "") if reflection else ""
            execution_check = str(reflection["execution_check"] or "") if reflection else ""
            adjustment = str(reflection["adjustment"] or "") if reflection else ""
            previous_reflection = self.learning_previous_reflection_row(user, int(row["learning_task_id"]))
            is_compare_review = previous_reflection is not None
            compare_html = self.learning_review_compare_html(user, int(row["learning_task_id"]), str(row["template"] or ""))
            recommended_next_html = self.learning_recommended_next_html(user, ai_ready, int(row["learning_task_id"])) if reflection else ""
            saved_html = (
                '<div class="saved-reflection">'
                "<strong>已保存复盘</strong>"
                f"<p><b>我想练什么:</b> {escape(hypothesis or '未填写')}</p>"
                f"<p><b>我有没有按规则做:</b> {escape(execution_check or '未填写')}</p>"
                f"<p><b>下次先改哪一点:</b> {escape(adjustment or '未填写')}</p>"
                "</div>"
                if reflection
                else ""
            )
            quick_review_html = (
                '<div class="review-done-note">'
                "<strong>你刚完成了第一次学习闭环</strong>"
                "<p>你已经解锁 6/6。这条记录已经把目标、练习、观察和复盘串起来。今天的第一圈已经达标,现在可以停在这里;下面的三问以后还能继续修改。</p>"
                '<div class="review-done-list">'
                "<div><b>你完成了什么</b><span>选目标、看拆解、生成练习、生成观察记录、保存复盘。</span></div>"
                "<div><b>这次先记住</b><span>复盘不是猜对错,而是记录想练什么、有没有按规则做、下次改哪一点。</span></div>"
                "<div><b>下一次比较</b><span>换一个任务后,观察它和这一关哪里不同。</span></div>"
                "</div>"
                '<div class="review-done-actions">'
                '<a class="btn blue" href="#learning-loop">查看 6/6 成就</a>'
                '<a class="btn secondary" href="#learning-journey">回到学习轨迹</a>'
                "</div>"
                f"{recommended_next_html}"
                "</div>"
                if reflection
                else (
                    '<div class="review-start">'
                    f"<strong>{'第二关先做对照复盘' if is_compare_review else '第一次复盘不用写专业分析'}</strong>"
                    f"<p>{'这次只比较上一关和这一关哪里不同。不会写时可以先保存对照示例,之后再改成自己的话。' if is_compare_review else '你只需要留下第一条学习记录。不会写时可以先保存示例;点击后就完成 6/6,之后还能改成自己的话。'}</p>"
                    '<form method="post" action="/learn/reflections/quick-save">'
                    f'{csrf_input(user)}<input type="hidden" name="practice_signal_id" value="{signal_id}">'
                    f'<button class="blue" type="submit">{"先用对照示例复盘" if is_compare_review else "一键完成 6/6 并保存示例复盘"}</button>'
                    "</form>"
                    "</div>"
                )
            )
            submit_label = "更新复盘" if reflection else "保存我的复盘"
            manual_title = "想修改自己的三句话? 展开手写复盘" if reflection else ("想自己写对照? 展开三问" if is_compare_review else "想自己写? 展开手写三句话")
            manual_hint = (
                "已经有一条复盘记录,需要时再展开修改。"
                if reflection
                else ("第二关可以先点上面的对照示例;想自己比较时再展开这里。" if is_compare_review else "第一次可以直接点上面的示例复盘;会写时再展开这里。")
            )
            if reflection:
                card_title = "对照复盘已保存" if is_compare_review else "第一次复盘已保存"
                status_badge = "已完成复盘"
            else:
                card_title = "完成第二关对照复盘" if is_compare_review else "先完成第一次复盘"
                status_badge = "已生成观察记录"
            question_items = (
                (
                    "这一关和上一关的观察角度哪里不同?",
                    "我有没有继续用小数量和同样的边界?",
                    "上次想改的小动作这次有没有用上?",
                )
                if is_compare_review
                else (
                    "我这次到底想练什么?",
                    "我有没有按小数量和边界规则做?",
                    "下次我先改哪一个小动作?",
                )
            )
            placeholders = (
                (
                    "例如: 上一关看反转,这一关看动量,我想比较两种观察角度哪里不同。",
                    "例如: 我仍然只用 100 的模拟数量,没有临时加大练习规模。",
                    "例如: 上次提醒我要先看依据,这次我会继续避免只盯着涨跌。",
                )
                if is_compare_review
                else (
                    "例如: 我想练习把一个观察想法记录下来,不是预测它一定上涨。",
                    "例如: 我只用了 100 的模拟数量,没有临时加大练习规模。",
                    "例如: 下次先比较两个候选的依据,不要只看涨跌。",
                )
            )
            completion_first_html = quick_review_html if reflection else ""
            review_prompt_html = "" if reflection else quick_review_html
            cards.append(
                '<article class="review-card">'
                f"{completion_first_html}"
                f'<p><span class="badge">{escape(status_badge)}</span> <span class="badge">{escape(template)}</span></p>'
                f'<h3>{escape(card_title)}</h3>'
                '<div class="review-snapshot">'
                f'<div><span>观察材料</span><strong>{escape(title)}</strong><small>只作学习样本</small></div>'
                f'<div><span>练习规模</span><strong>{escape(observation_text)}</strong><small>模拟记录已生成</small></div>'
                '<div><span>复盘焦点</span><strong>先写三问</strong><small>先不判断涨跌</small></div>'
                f'<div><span>练习类型</span><strong>{escape(template)}</strong><small>只用于学习复盘</small></div>'
                "</div>"
                '<details class="practice-detail">'
                '<summary>可选:查看代码和模拟记录价<span>复盘先写三句大白话;价格细节可以稍后看。</span></summary>'
                '<div class="practice-detail-grid">'
                f'<div><b>代码</b><span>{escape(row["code"])}</span></div>'
                f'<div><b>模拟记录价</b><span>{executed_price}</span></div>'
                "</div></details>"
                f'<p class="muted">{escape(learning_display_rationale(row["rationale"]))}</p>'
                f"{compare_html}"
                f"{review_prompt_html}"
                f"{saved_html}"
                '<details class="manual-reflection">'
                f'<summary>{escape(manual_title)}<span>{escape(manual_hint)}</span></summary>'
                '<div class="manual-reflection-body">'
                '<p><strong>只写三句大白话就够了</strong></p>'
                '<ol class="review-questions">'
                f'<li>{escape(question_items[0])}</li>'
                f'<li>{escape(question_items[1])}</li>'
                f'<li>{escape(question_items[2])}</li>'
                '</ol>'
                '<form class="reflection-form" method="post" action="/learn/reflections">'
                f'{csrf_input(user)}<input type="hidden" name="practice_signal_id" value="{signal_id}">'
                f'<label>1. 我这次想练什么</label><textarea name="hypothesis" maxlength="700" placeholder="{escape(placeholders[0], quote=True)}">{escape(hypothesis)}</textarea>'
                f'<label>2. 我有没有按小数量规则做</label><textarea name="execution_check" maxlength="700" placeholder="{escape(placeholders[1], quote=True)}">{escape(execution_check)}</textarea>'
                f'<label>3. 下次先改哪一点</label><textarea name="adjustment" maxlength="700" placeholder="{escape(placeholders[2], quote=True)}">{escape(adjustment)}</textarea>'
                f'<p><button type="submit">{submit_label}</button></p>'
                "</form>"
                "</div>"
                "</details>"
                f'<p><a href="/learn/tasks/{int(row["learning_task_id"])}">回看教练拆解</a></p>'
                "</article>"
            )
        optional_actions_html = (
            f"""
  <details class="advanced-practice review-optional-actions">
    <summary>可选:AI 复盘和高级入口<span>第一次先不用展开;先点上方蓝色复盘按钮。AI 复盘和高级模拟盘都不是完成第一圈的前置条件。</span></summary>
    <div class="advanced-practice-body">
      <p><a class="btn secondary" href="/account/ai">{ai_review_label}</a></p>
    </div>
  </details>
"""
            if first_unreviewed is not None
            else f"""
  <p class="muted">下面是可选入口,第一次可以先不点。</p>
  <p><a class="btn secondary" href="/account/ai">{ai_review_label}</a> <a class="btn secondary" href="/app">查看高级模拟盘细节</a></p>
"""
        )
        return f"""
<section class="card" id="learning-review">
  <h2>{escape(section_title)}</h2>
  <p>{escape(section_intro)}</p>
  {focus_html}
  {ready_html}
  <div class="review-cards">{''.join(cards)}</div>
  {optional_actions_html}
</section>
"""

    def learning_review_compare_html(self, user, current_task_id: int, current_template: str) -> str:
        previous = self.learning_previous_reflection_row(user, current_task_id)
        if previous is None:
            return ""
        previous_template = services.LEARNING_TEMPLATES.get(str(previous["template"] or ""), str(previous["template"] or "上一关"))
        current_label = services.LEARNING_TEMPLATES.get(str(current_template or ""), str(current_template or "这一关"))
        previous_adjustment = str(previous["adjustment"] or "上一关还没有写下次要改什么。")
        return f"""
<div class="review-compare">
  <strong>这次复盘要和上一关比较</strong>
  <p>上一关: {escape(previous_template)}; 这一关: {escape(current_label)}。先比较方法差异,不要急着看涨跌。</p>
  <p class="muted">只比较三件事:候选来源、数量边界、上次想改的小动作有没有用上。</p>
  <div class="review-compare-grid">
    <div><b>候选怎么来?</b><span>上一关看一种观察角度,这一关换一个角度再跑一次。</span></div>
    <div><b>边界是否一样?</b><span>数量、对象数量和风险边界尽量保持可比较。</span></div>
    <div><b>上次想改的小动作</b><span>{escape(previous_adjustment)}</span></div>
  </div>
</div>
"""

    def learning_journey_html(self, user) -> str:
        rows = self.con.execute(
            """
            SELECT
                t.id,
                t.goal,
                t.template,
                t.created_at,
                (SELECT COUNT(*) FROM learning_tasks p WHERE p.user_id=t.user_id AND p.id<=t.id) AS sequence,
                COUNT(DISTINCT CASE WHEN s.status IN ('pending','executed') THEN s.id END) AS signal_count,
                SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN s.status='executed' THEN 1 ELSE 0 END) AS executed_count,
                COUNT(DISTINCT r.id) AS reflection_count,
                (
                    SELECT lr.adjustment
                    FROM learning_reflections lr
                    JOIN practice_signals ls ON ls.id=lr.practice_signal_id AND ls.user_id=lr.user_id
                    WHERE lr.user_id=t.user_id AND ls.learning_task_id=t.id
                    ORDER BY lr.updated_at DESC, lr.id DESC
                    LIMIT 1
                ) AS latest_adjustment
            FROM learning_tasks t
            LEFT JOIN practice_signals s ON s.learning_task_id=t.id AND s.user_id=t.user_id
            LEFT JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=t.user_id
            WHERE t.user_id=?
            GROUP BY t.id
            ORDER BY t.id DESC
            LIMIT 6
            """,
            (int(user["id"]),),
        ).fetchall()
        if not rows:
            return ""
        summary = self.con.execute(
            """
            SELECT
                COUNT(DISTINCT t.id) AS task_count,
                COUNT(DISTINCT CASE WHEN s.status IN ('pending','executed') THEN s.id END) AS signal_count,
                COUNT(DISTINCT CASE WHEN s.status='pending' THEN s.id END) AS pending_count,
                COUNT(DISTINCT CASE WHEN s.status='executed' THEN s.id END) AS executed_count,
                COUNT(DISTINCT CASE WHEN s.status='executed' AND r.id IS NULL THEN s.id END) AS unreflected_count,
                COUNT(DISTINCT r.id) AS reflection_count,
                COUNT(DISTINCT CASE WHEN s.status IN ('pending','executed') THEN t.template END) AS template_count,
                GROUP_CONCAT(DISTINCT CASE WHEN s.status IN ('pending','executed') THEN t.template END) AS templates
            FROM learning_tasks t
            LEFT JOIN practice_signals s ON s.learning_task_id=t.id AND s.user_id=t.user_id
            LEFT JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=t.user_id
            WHERE t.user_id=?
            """,
            (int(user["id"]),),
        ).fetchone()
        task_count = int(summary["task_count"] or 0) if summary is not None else len(rows)
        signal_total = int(summary["signal_count"] or 0) if summary is not None else 0
        pending_total = int(summary["pending_count"] or 0) if summary is not None else 0
        unreflected_total = int(summary["unreflected_count"] or 0) if summary is not None else 0
        reflection_total = int(summary["reflection_count"] or 0) if summary is not None else 0
        template_count = int(summary["template_count"] or 0) if summary is not None else 0
        template_keys = [item for item in str(summary["templates"] or "").split(",") if item] if summary is not None else []
        template_labels = [services.LEARNING_TEMPLATES.get(key, key) for key in template_keys]
        templates_text = "还没开始"
        if template_labels:
            templates_text = "、".join(template_labels[:3])
            if len(template_labels) > 3:
                templates_text += f" 等 {len(template_labels)} 种"
        latest = rows[0]
        if unreflected_total > 0:
            next_suggestion = "先完成待复盘的观察,用三问写下下次改什么。"
            next_action_href = "#learning-review"
            next_action_label = "去完成三问复盘"
        elif pending_total > 0:
            next_suggestion = "先把待观察练习生成一条观察记录,再回来做三问复盘。"
            next_action_href = "#today-practice"
            next_action_label = "去今日练习"
        elif int(latest["signal_count"] or 0) == 0:
            next_suggestion = "把最新目标生成今日练习,不用一次学太多。"
            next_action_href = f"/learn/tasks/{int(latest['id'])}"
            next_action_label = "生成今日练习"
        elif reflection_total >= 2:
            next_suggestion = "进入第三关风险边界,把两次策略对照里的数量、回撤和停止条件讲清楚。"
            next_action_href = "#learning-loop"
            next_action_label = "查看下一关建议"
        elif reflection_total == 1:
            next_suggestion = "第一圈已完成,可以先停在这里;想巩固时再开第二关动量对照。"
            next_action_href = "#learning-loop"
            next_action_label = "可选:查看第二关建议"
        else:
            next_suggestion = "先完成第一条练习和复盘,把学习闭环跑通。"
            next_action_href = "#learning-loop"
            next_action_label = "继续第一圈"
        latest_reflection_row = self.con.execute(
            """
            SELECT
                t.template,
                r.hypothesis,
                r.adjustment
            FROM learning_reflections r
            JOIN practice_signals s ON s.id=r.practice_signal_id AND s.user_id=r.user_id
            JOIN learning_tasks t ON t.id=s.learning_task_id AND t.user_id=s.user_id
            WHERE r.user_id=?
            ORDER BY r.updated_at DESC, r.id DESC
            LIMIT 1
            """,
            (int(user["id"]),),
        ).fetchone()
        last_takeaway_html = ""
        if latest_reflection_row is not None:
            latest_template = services.LEARNING_TEMPLATES.get(
                str(latest_reflection_row["template"] or ""),
                str(latest_reflection_row["template"] or "上一关"),
            )
            latest_hypothesis = str(latest_reflection_row["hypothesis"] or "上一关已经留下想练什么。").strip()
            latest_adjustment = str(latest_reflection_row["adjustment"] or "下次继续把依据、数量和风险边界写清楚。").strip()
            last_takeaway_html = f"""
    <div class="journey-summary-takeaway">
      <b>下次回来先看这里</b>
      <p><span>{escape(latest_template)}</span>{escape(latest_adjustment)}</p>
      <small>{escape(latest_hypothesis)}</small>
    </div>
"""
        upgrade_html = ""
        if reflection_total >= 2:
            reflected_rows = self.con.execute(
                """
                SELECT
                    t.id,
                    t.template,
                    (
                        SELECT lr.adjustment
                        FROM learning_reflections lr
                        JOIN practice_signals ls ON ls.id=lr.practice_signal_id AND ls.user_id=lr.user_id
                        WHERE lr.user_id=t.user_id AND ls.learning_task_id=t.id
                        ORDER BY lr.updated_at DESC, lr.id DESC
                        LIMIT 1
                    ) AS latest_adjustment
                FROM learning_tasks t
                WHERE t.user_id=?
                  AND EXISTS (
                      SELECT 1
                      FROM practice_signals s
                      JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=s.user_id
                      WHERE s.user_id=t.user_id AND s.learning_task_id=t.id
                  )
                ORDER BY t.id DESC
                LIMIT 2
                """,
                (int(user["id"]),),
            ).fetchall()
            if len(reflected_rows) >= 2:
                older, newer = list(reversed(reflected_rows))
                older_template = services.LEARNING_TEMPLATES.get(str(older["template"] or ""), str(older["template"] or "第一关"))
                newer_template = services.LEARNING_TEMPLATES.get(str(newer["template"] or ""), str(newer["template"] or "第二关"))
                older_adjustment = str(older["latest_adjustment"] or "第一关留下了下次要改什么。")
                newer_adjustment = str(newer["latest_adjustment"] or "第二关留下了下次要改什么。")
                upgrade_html = f"""
    <div class="journey-upgrade">
      <div class="journey-upgrade-head">
        <div>
          <span class="tag">LEVEL UP</span>
          <strong>策略视角对照已解锁</strong>
          <p>你已经不只是完成练习,而是比较过两种观察角度。下一步要把“怎么控制风险”补上,学习才不会只停留在看涨跌。</p>
        </div>
        <div class="journey-upgrade-badge">2 VIEW</div>
      </div>
      <div class="journey-upgrade-grid">
        <div><b>第一种视角: {escape(older_template)}</b><p>{escape(older_adjustment)}</p></div>
        <div><b>第二种视角: {escape(newer_template)}</b><p>{escape(newer_adjustment)}</p></div>
      </div>
    </div>
"""
        summary_html = f"""
  <div class="journey-summary" aria-label="学习轨迹摘要">
    <div class="journey-summary-head">
      <div>
        <span class="tag">Learning Summary</span>
        <strong>学习轨迹摘要</strong>
        <p>已经创建 {task_count} 个学习任务。先看完成度和下一步,不用在复杂表格里找线索。</p>
      </div>
      <div class="journey-summary-next">
        <b>下一步建议</b>
        <span>{escape(next_suggestion)}</span>
        <a class="btn secondary" href="{escape(next_action_href, quote=True)}">{escape(next_action_label)}</a>
      </div>
    </div>
    <div class="journey-summary-grid">
      <div><b>{reflection_total}</b><span>已完成复盘</span></div>
      <div><b>{template_count}</b><span>练过的模板</span></div>
      <div><b>{signal_total}</b><span>累计练习计划</span></div>
    </div>
    {last_takeaway_html}
    <p class="journey-summary-templates"><b>练过的模板</b>{escape(templates_text)}</p>
    {upgrade_html}
  </div>
"""
        items = []
        for row in reversed(rows):
            task_id = int(row["id"])
            sequence = int(row["sequence"] or 0)
            signal_count = int(row["signal_count"] or 0)
            pending_count = int(row["pending_count"] or 0)
            executed_count = int(row["executed_count"] or 0)
            reflection_count = int(row["reflection_count"] or 0)
            latest_adjustment = str(row["latest_adjustment"] or "").strip()
            template = services.LEARNING_TEMPLATES.get(str(row["template"] or ""), str(row["template"] or "学习练习"))
            if reflection_count > 0:
                status = "已复盘"
                status_class = "journey-status-done"
                hint = "这一关已经留下学习记录,可以回看拆解或继续下一关。"
                action_href = f"/learn/tasks/{task_id}"
                action_label = "回看拆解"
            elif executed_count > 0:
                status = "待复盘"
                status_class = "journey-status-now"
                hint = "已经生成观察记录,下一步先保存三问复盘。"
                action_href = "#learning-review"
                action_label = "去复盘"
            elif pending_count > 0:
                status = "待观察"
                status_class = "journey-status-now"
                hint = "已经生成今日练习,下一步确认观察材料、练习规模和依据后生成观察记录。"
                action_href = "#today-practice"
                action_label = "生成观察记录"
            elif signal_count > 0:
                status = "练习已保存"
                status_class = "journey-status-wait"
                hint = "练习已有记录,回到任务页确认是否还要补充新的草稿。"
                action_href = f"/learn/tasks/{task_id}"
                action_label = "查看任务"
            else:
                status = "待生成练习"
                status_class = "journey-status-wait"
                hint = "先把教练拆解变成今日练习。"
                action_href = f"/learn/tasks/{task_id}"
                action_label = "生成练习"
            reflection_html = (
                '<div class="journey-reflection">'
                "<b>这一关下次想改</b>"
                f"<p>{escape(latest_adjustment)}</p>"
                "</div>"
                if latest_adjustment
                else ""
            )
            items.append(
                '<div class="journey-item">'
                f'<div class="journey-step">第 {sequence} 关</div>'
                '<div class="journey-main">'
                f'<strong>{escape(row["goal"])}</strong>'
                f'<p>{escape(hint)}</p>'
                '<div class="journey-meta">'
                f'<span class="badge">{escape(template)}</span>'
                f'<span class="badge {status_class}">{escape(status)}</span>'
                f'<span class="badge">练习 {signal_count}</span>'
                f'<span class="badge">复盘 {reflection_count}</span>'
                "</div>"
                f"{reflection_html}"
                "</div>"
                f'<div class="journey-action"><a class="btn secondary" href="{escape(action_href, quote=True)}">{escape(action_label)}</a></div>'
                "</div>"
            )
        return f"""
<section class="card journey-card" id="learning-journey">
  <h2>我的学习轨迹</h2>
  <p>把每一关的目标、练习和复盘串起来看。复盘后的“下次改什么”会留在这里,下一关就能拿来比较。</p>
  {summary_html}
  <div class="journey-list">{''.join(items)}</div>
</section>
"""

    def render_learn(self, user, query):
        key_row = ai_service.get_key_row(self.con, user["id"])
        ai_ready = key_row is not None and bool(int(key_row["enabled"]))
        task_rows = services.learning_tasks(self.con, user["id"], limit=6)
        has_tasks = bool(task_rows)
        reflection_count = int(
            self.con.execute(
                "SELECT COUNT(*) FROM learning_reflections WHERE user_id=?",
                (int(user["id"]),),
            ).fetchone()[0]
        )
        progress_html = self.learning_loop_progress_html(user, ai_ready)
        starter_html = self.learning_starter_choices_html(user, ai_ready, has_tasks)
        continue_task_html = self.learning_continue_task_html(user)
        today_practice_html = self.learning_today_practice_html(user)
        recent_review_html = self.learning_recent_review_html(user)
        journey_html = self.learning_journey_html(user)
        preset_section_id = "more-presets" if starter_html else "learn-presets"
        preset_title = "想换关? 更多新手关卡" if starter_html else "不知道问什么? 从关卡地图开始"
        task_log_html = ""
        if has_tasks:
            task_log_intro = "日常只看上面的「我的学习轨迹」就够了。下面保留任务 ID、状态和创建时间,方便以后排查或回看。"
            tasks_html = "".join(
                labeled_table_row(
                    [
                        ("ID", f'<a href="/learn/tasks/{int(task["id"])}">#{int(task["id"])}</a>'),
                        ("目标", escape(task["goal"])),
                        ("模板", escape(services.LEARNING_TEMPLATES.get(task["template"], task["template"]))),
                        ("状态", escape(task["status"])),
                        ("创建时间", escape(task["created_at"])),
                    ]
                )
                for task in task_rows
            )
            task_log_html = f"""
<section class="card">
  <h2>学习记录</h2>
  <p>{escape(task_log_intro)}</p>
  <details class="advanced-practice">
    <summary>查看高级任务记录<span>新手第一次闭环可以先不展开;这里是更像后台记录的明细表。</span></summary>
    <div class="advanced-practice-body">
      <table class="mobile-card-table"><thead><tr><th>ID</th><th>目标</th><th>模板</th><th>状态</th><th>创建时间</th></tr></thead><tbody>{tasks_html}</tbody></table>
    </div>
  </details>
</section>
"""
        preset_cards_html = self.learning_preset_cards(user, ai_ready)
        if starter_html:
            preset_library_html = f"""
  <p>第一次先用上面的蓝色推荐按钮。下面是分级关卡地图,想换关时再展开;没完成第一圈前先不要跨到第二关。</p>
  <details class="advanced-practice preset-library">
    <summary>可选:展开新手关卡地图<span>第一次先不用展开;不知道怎么选时,直接点上面的蓝色按钮。</span></summary>
    <div class="advanced-practice-body">
      {preset_cards_html}
    </div>
  </details>
"""
        elif has_tasks and reflection_count <= 0:
            preset_library_html = f"""
		  <p>你已经有一个学习任务在进行中。先完成当前第一圈:生成练习、生成观察记录、保存复盘;完成 6/6 后再换关。</p>
  <details class="advanced-practice preset-library">
    <summary>可选:完成当前第一圈后再换关<span>现在先不用展开;当前任务完成复盘前,新题会分散注意力。</span></summary>
    <div class="advanced-practice-body">
      {preset_cards_html}
    </div>
  </details>
"""
        else:
            preset_library_html = f"""
  <p>上面三个入口适合继续学习;这里保留完整关卡地图。已配置 key 时会调用 AI 创建任务;没配置 key 时会创建内置示例教练任务,不调用 DeepSeek。</p>
  {preset_cards_html}
"""
        first_loop_in_progress = has_tasks and reflection_count <= 0
        if continue_task_html:
            current_step_href = "#continue-learning-task"
            current_step_label = "回到当前任务"
        elif today_practice_html:
            current_step_href = "#first-practice-card"
            current_step_label = "生成观察记录"
        elif recent_review_html:
            current_step_href = "#learning-review"
            current_step_label = "保存复盘"
        else:
            current_step_href = "#learning-loop"
            current_step_label = "查看当前进度"
        if reflection_count > 0:
            mobile_next_bar_html = ""
        elif not has_tasks:
            mobile_next_bar_html = """
<div class="mobile-next-spacer" aria-hidden="true"></div>
<div class="mobile-next-bar" role="navigation" aria-label="手机下一步提示">
  <div><span>现在只做一件事</span><b>先开始第一关</b></div>
  <a class="btn blue" href="#learn-presets">去开始</a>
</div>
"""
        else:
            mobile_next_bar_html = f"""
<div class="mobile-next-spacer" aria-hidden="true"></div>
<div class="mobile-next-bar" role="navigation" aria-label="手机下一步提示">
  <div><span>下一步</span><b>{escape(current_step_label)}</b></div>
  <a class="btn blue" href="{escape(current_step_href, quote=True)}">继续</a>
</div>
"""
        beginner_focus_html = self.learning_beginner_focus_html(
            has_tasks,
            reflection_count,
            continue_task_html,
            today_practice_html,
            recent_review_html,
        )
        if first_loop_in_progress:
            coach_status = "AI 教练已配置,但第一圈先不要新建第二个目标。" if ai_ready else "没有 DeepSeek key 也能完成当前第一圈。"
            custom_task_html = f"""
    <h2>当前第一圈先不新建任务</h2>
    <div class="msg">
	      你已经开始第一次学习闭环。现在先回到页面上方的当前步骤,完成生成练习、生成观察记录和保存复盘;完成 6/6 后再换关或配置 AI。
    </div>
    <div class="task-action-points">
      <div><b>现在怎么继续</b><p>只看页面上方的蓝色主按钮,不要再开新题。</p></div>
      <div><b>什么时候配 key</b><p>{escape(coach_status)}</p></div>
      <div><b>完成标准</b><p>看到 6/6 和一条复盘记录,第一圈才算完成。</p></div>
    </div>
    <p><a class="btn blue" href="{escape(current_step_href, quote=True)}">{escape(current_step_label)}</a> <a class="btn secondary" href="/account/ai">AI 教练配置(稍后再看)</a></p>
"""
        else:
            if ai_ready:
                ai_custom_form_html = f"""
    <form method="post" action="/learn/coach">
      {csrf_input(user)}
      <label>我想学习或练习的目标</label>
      <textarea name="goal" maxlength="500" required placeholder="例如: 我想学习如何用 AI 帮我设计一个低风险的量化练习,并知道应该看哪些指标。"></textarea>
      <div class="row">
        <div><label>当前基础</label><select name="difficulty">{self.learning_difficulty_options()}</select></div>
        <div><label>练习模板</label><select name="template">{self.learning_template_options()}</select></div>
      </div>
      <p><button type="submit">让 AI 拆解目标</button></p>
    </form>
"""
                if not has_tasks and reflection_count <= 0:
                    custom_task_html = f"""
    <h2>AI key 已配置,第一圈仍建议用推荐任务</h2>
    <div class="msg">
      你已经可以使用 AI 教练。第一次先不要自己写提示词,直接用上方蓝色推荐按钮跑完 6/6;想自己提目标时再展开下面的输入框。
    </div>
    <details class="advanced-practice custom-ai-goal">
      <summary>可选:自己写学习目标<span>第一次可以先不展开;不知道怎么问时,先用推荐任务更快完成第一圈。</span></summary>
      <div class="advanced-practice-body">
        {ai_custom_form_html}
      </div>
    </details>
"""
                else:
                    custom_task_html = f"""
    <h2>创建自定义 AI 学习任务</h2>
    <div class="msg">
      DeepSeek API key 已配置。你可以输入自己的学习目标,AI 会先做方法拆解,不会替你下单。
    </div>
    {ai_custom_form_html}
"""
            else:
                if reflection_count > 0:
                    custom_task_html = f"""
    <h2>第一圈已完成,AI key 是可选升级</h2>
    <div class="msg">
      你已经完成第一次学习闭环。现在可以先停在这里,也可以换一个预设跑第二关;配置 AI key 只是为了写自定义目标和 AI 复盘,不是继续学习的前置条件。
    </div>
    <div class="task-action-points">
      <div><b>现在怎么继续</b><p>先看上方的学习成果和学习轨迹,再决定要不要打开第二关建议。</p></div>
      <div><b>什么时候配 key</b><p>想自己写目标、让 AI 拆解或复盘时再配置。</p></div>
      <div><b>费用边界</b><p>不配置 key 也能继续使用示例教练和模拟练习。</p></div>
    </div>
    <p><a class="btn" href="#learning-loop">查看第二关建议</a> <a class="btn secondary" href="#{preset_section_id}">选择新任务</a> <a class="btn secondary" href="/account/ai">配置 AI 教练(可选)</a></p>
"""
                else:
                    custom_task_html = """
    <h2>第一圈不用 DeepSeek key</h2>
    <div class="msg">
      你现在可以直接开始:点击上面的蓝色推荐按钮,系统会创建「示例教练任务」,不调用 DeepSeek、不产生 AI 费用。
      等你完成第一次闭环后,如果想写自己的目标再让 AI 拆解,再配置 key。
    </div>
    <div class="task-action-points">
      <div><b>现在怎么开始</b><p>先看上面的 30 秒概念,再点蓝色推荐按钮;不用 key 也能生成示例任务。</p></div>
      <div><b>什么时候配 key</b><p>完成第一圈后,想写自己的目标再去 AI 教练配置。</p></div>
      <div><b>费用边界</b><p>示例教练不调用 DeepSeek,不会消耗你的 API 额度。</p></div>
    </div>
	    <p><a class="btn" href="#learn-presets">回到蓝色推荐按钮</a> <a class="btn secondary" href="/learn/demo">看 3 分钟示例</a> <a class="btn secondary" href="/account/ai">以后再配置 AI key</a></p>
	"""
        advanced_intro_link = (
            '<a class="btn secondary" href="/app">高级模拟盘</a>'
            if reflection_count > 0
            else '<span class="badge">6/6 前先不要进入高级模拟盘</span>'
        )
        advanced_intro_text = (
            "第一次闭环已经完成,可以进入高级模拟盘查看账户、持仓和成交细节。"
            if reflection_count > 0
            else "高级模拟盘入口会在完成 6/6 后出现。完成后页面会再给你高级入口。"
        )
        boundary_actions_html = (
            '<p><a class="btn secondary" href="/app">高级模拟盘</a> <a class="btn secondary" href="/account/ai">AI 教练配置</a></p>'
            if reflection_count > 0
            else '<p><span class="badge">高级模拟盘入口会在完成 6/6 后出现</span> <a class="btn secondary" href="/account/ai">AI 教练配置(可选,以后再看)</a></p>'
        )
        body = f"""
	{self.message_html(query)}
{recent_review_html}
{today_practice_html}
{continue_task_html}
{starter_html}
{beginner_focus_html}
{progress_html}
{journey_html}
	<section class="card">
	  <h2>新手 AI 学习工作台</h2>
	  <p>这里先帮你理解量化投资的基本环节,再把一个学习目标拆成可演练、可记录、可复盘的模拟盘任务。</p>
	  <p><a class="btn" href="/learn/demo">3 分钟示例体验</a> <a class="btn secondary" href="/lessons">先看:量化三大坑(免登录、免 key)</a> {advanced_intro_link}</p>
	  <p class="muted">第一次不需要进入高级模拟盘。先在本页完成选目标、生成练习、生成观察记录和三问复盘,再去看账户、持仓和成交细节。{advanced_intro_text}</p>
  <div class="flow-map">
    <div class="flow-step"><span>STEP 1</span><strong>量化投资是什么</strong><p>用数据、规则和程序把投资想法转成可检验流程。它不是 AI 替你预测涨跌,也不是自动稳赚。</p></div>
    <div class="flow-step"><span>STEP 2</span><strong>你需要理解哪些环节</strong><p>数据、交易规则、策略假设、回测陷阱、模拟盘执行和复盘,分别解决不同问题。</p></div>
    <div class="flow-step"><span>STEP 3</span><strong>目标如何拆成任务</strong><p>把“我想学习量化”拆成一个观察模板、几个参数、一组记录问题和一次可复盘练习。</p></div>
    <div class="flow-step"><span>STEP 4</span><strong>以练代学</strong><p>AI 先解释方法,系统生成候选草稿,你确认后保存为待执行计划,再用模拟结果复盘。</p></div>
  </div>
</section>
<section class="card" id="{preset_section_id}">
  <h2>{preset_title}</h2>
  {preset_library_html}
</section>
<section class="grid">
  <div class="card">
    {custom_task_html}
  </div>
  <div class="card">
    <h2>新手边界</h2>
    <ul class="guide-list">
      <li>AI 只做方法教学、目标拆解和草稿说明。</li>
      <li>具体候选由系统按行情/预测数据确定,不是模型荐股。</li>
      <li>保存后只是待执行计划,不会自动成交。</li>
	      <li>执行、取消、复盘都由你自己确认。</li>
	    </ul>
	    {boundary_actions_html}
	  </div>
	</section>
{task_log_html}
{mobile_next_bar_html}
	"""
        self.send_html("学习工作台", body, user=user)

    def learning_preview_rows_html(self, rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td data-label=\"代码\">{escape(row['code'])}</td>"
            f"<td data-label=\"名称\">{escape(row.get('name') or '-')}</td>"
            f"<td data-label=\"观察动作\">{escape(learning_observation_label(row['side']))}</td>"
            f"<td data-label=\"模拟观察数量\">{escape(learning_observation_action(row['side'], int(row['qty'])))}</td>"
            f"<td data-label=\"依据\">{escape(learning_display_rationale(row.get('rationale')))}</td>"
            "</tr>"
            for row in rows
        )

    def learning_task_digest_html(self, task, saved_count: int) -> str:
        template = services.normalize_learning_template(task["template"] or "reversal")
        template_label = services.LEARNING_TEMPLATES.get(template, template)
        coach_label = self.learning_task_coach_label(task)
        goal = " ".join(str(task["goal"] or "这一关的学习目标").split())
        if len(goal) > 118:
            goal = goal[:115].rstrip() + "..."
        template_titles = {
            "reversal": "用反转观察理解“规则如何变成练习”",
            "momentum": "用动量观察理解“趋势不是确定预测”",
            "prediction": "把模型候选当成学习材料",
            "risk_review": "先把风险边界说清楚",
        }
        template_notes = {
            "reversal": "看短期波动后的修复假设,重点是记录依据和边界,不是猜明天涨跌。",
            "momentum": "看短期强势能否延续的观察假设,重点是避免追涨冲动和事后找理由。",
            "prediction": "把预测候选当作解释材料,重点看预测、行情、规则和复盘怎样连接。",
            "risk_review": "这一关不追求买卖动作,重点写清数量、回撤和停止条件。",
        }
        if saved_count > 0:
            next_text = "这关已经生成过今日练习。现在先回学习工作台,不要重复保存新草稿。"
        else:
            next_text = "先点页面上方蓝色按钮生成 1 条今日练习,第一次不用展开进阶设置。"
        return f"""
  <div class="coach-digest" aria-label="教练拆解摘要">
    <div class="coach-digest-main">
      <span class="tag">30 秒摘要</span>
      <strong>{escape(template_titles.get(template, f"用{template_label}完成一次入门练习"))}</strong>
      <p>{escape(template_notes.get(template, "先把目标变成一条小数量模拟观察,再用复盘检查自己是否理解。"))}</p>
    </div>
    <div class="coach-digest-side">
      <b>先看这三件事</b>
      <p>下面是把{escape(coach_label)}长文压缩成的新手读法。</p>
      <div class="coach-digest-steps">
        <div><strong>这一关目标</strong><small>{escape(goal)}</small></div>
        <div><strong>系统会做</strong><small>用{escape(template_label)}生成小数量模拟练习,不自动成交。</small></div>
        <div><strong>你下一步</strong><small>{escape(next_text)}</small></div>
      </div>
    </div>
  </div>
"""

    def learning_task_mobile_next_bar_html(self, user, task) -> str:
        task_id = int(task["id"])
        summary = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM practice_signals WHERE user_id=? AND learning_task_id=? AND status='pending') AS pending_count,
                (SELECT COUNT(*) FROM practice_signals WHERE user_id=? AND learning_task_id=? AND status='executed') AS executed_count,
                (SELECT COUNT(*) FROM learning_reflections WHERE user_id=? AND learning_task_id=?) AS reflection_count
            """,
            (int(user["id"]), task_id, int(user["id"]), task_id, int(user["id"]), task_id),
        ).fetchone()
        pending_count = int(summary["pending_count"] or 0) if summary else 0
        executed_count = int(summary["executed_count"] or 0) if summary else 0
        reflection_count = int(summary["reflection_count"] or 0) if summary else 0
        if reflection_count > 0 or str(task["status"] or "") == "completed":
            step_label = "已完成"
            action_label = "看学习轨迹"
            href = "/learn#learning-journey"
        elif executed_count > 0:
            step_label = "最后一步"
            action_label = "去三问复盘"
            href = "/learn#learning-review"
        elif pending_count > 0:
            step_label = "下一步"
            action_label = "生成观察记录"
            href = "/learn#today-practice"
        else:
            step_label = "下一步"
            action_label = "生成今日练习"
            href = "#task-next-action"
        return f"""
<div class="mobile-next-spacer" aria-hidden="true"></div>
<div class="mobile-next-bar" role="navigation" aria-label="手机学习任务下一步提示">
  <div><span>{escape(step_label)}</span><b>{escape(action_label)}</b></div>
  <a class="btn blue" href="{escape(href, quote=True)}">继续</a>
</div>
"""

    def learning_task_bridge_html(self, user, task) -> str:
        task_id = int(task["id"])
        summary = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM learning_tasks WHERE user_id=? AND id < ?) AS previous_task_count,
                (SELECT COUNT(*) FROM learning_tasks WHERE user_id=? AND id <= ?) AS task_sequence,
                (SELECT COUNT(*) FROM learning_reflections WHERE user_id=?) AS reflection_count
            """,
            (int(user["id"]), task_id, int(user["id"]), task_id, int(user["id"])),
        ).fetchone()
        previous_task_count = int(summary["previous_task_count"] or 0)
        reflection_count = int(summary["reflection_count"] or 0)
        if previous_task_count <= 0 or reflection_count <= 0:
            return ""
        previous = self.con.execute(
            """
            SELECT id, goal, template
            FROM learning_tasks
            WHERE user_id=? AND id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user["id"]), task_id),
        ).fetchone()
        if previous is None:
            return ""
        task_sequence = int(summary["task_sequence"] or previous_task_count + 1)
        previous_template = services.LEARNING_TEMPLATES.get(previous["template"], previous["template"])
        current_template = services.LEARNING_TEMPLATES.get(task["template"], task["template"])
        if str(previous["template"]) == str(task["template"]):
            angle_text = f"上一关和这一关都是 {current_template},这次重点是换一个目标,看你能不能自己说清楚依据。"
        else:
            angle_text = f"上一关: {previous_template}; 这一关: {current_template}。先比较两种练习的观察角度,不要急着看涨跌。"
        if str(task["template"]) == "risk_review":
            practice_text = "风险复盘不直接等于买入卖出。生成今日练习时,系统会用一条小数量观察作为复盘材料。"
        else:
            practice_text = "这次仍然只生成小数量模拟练习。保存后先回学习工作台点“生成观察记录”,再写三问复盘。"
        points = (
            ("对比什么", angle_text),
            ("这次怎么练", practice_text),
            ("完成标准", "不是赚多少钱,而是能写出这次和上次想练什么、规则有没有守住、下次改什么。"),
        )
        point_html = "".join(
            f'<div class="bridge-point"><span>{escape(title)}</span><p>{escape(text)}</p></div>'
            for title, text in points
        )
        compare_html = ""
        if str(previous["template"]) == "reversal" and str(task["template"]) == "momentum":
            compare_html = """
  <div class="bridge-compare">
    <h3>为什么这次换成动量观察?</h3>
    <div class="bridge-compare-grid">
      <div><b>上一关练的是反转</b><p>看“跌多了会不会修复一点”,重点是不要把短期反弹当成确定预测。</p></div>
      <div><b>这一关练的是动量</b><p>看“强势会不会延续”,重点是记录依据,同时避免追涨冲动。</p></div>
      <div><b>只比较 3 件事</b><p>候选怎么来、数量边界是否一样、复盘时哪里需要修正。</p></div>
    </div>
  </div>
"""
        return f"""
<section class="card task-bridge">
  <div class="task-bridge-head">
    <div>
      <span class="tag">NEXT QUEST</span>
      <strong>这是你的第 {task_sequence} 个学习任务</strong>
      <p>你已经有 {reflection_count} 条复盘记录。现在要做的不是从头学,而是把上一关的经验带到这一关。</p>
    </div>
    <div class="task-bridge-score">+1 关</div>
  </div>
  <div class="bridge-points">{point_html}</div>
  {compare_html}
</section>
"""

    def learning_task_next_action_html(self, user, task) -> str:
        task_id = int(task["id"])
        task_template = services.normalize_learning_template(task["template"] or "reversal")
        coach_label = self.learning_task_coach_label(task)
        row = self.con.execute(
            """
            SELECT
                COUNT(DISTINCT s.id) AS signal_count,
                SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN s.status='executed' THEN 1 ELSE 0 END) AS executed_count,
                COUNT(DISTINCT r.id) AS reflection_count
            FROM learning_tasks t
            LEFT JOIN practice_signals s ON s.learning_task_id=t.id AND s.user_id=t.user_id
            LEFT JOIN learning_reflections r ON r.practice_signal_id=s.id AND r.user_id=t.user_id
            WHERE t.id=? AND t.user_id=?
            """,
            (task_id, int(user["id"])),
        ).fetchone()
        signal_count = int(row["signal_count"] or 0)
        pending_count = int(row["pending_count"] or 0)
        executed_count = int(row["executed_count"] or 0)
        reflection_count = int(row["reflection_count"] or 0)
        if reflection_count > 0:
            loop_completed = 6
            loop_hint = "已完成:理解概念、选择目标、获得教练拆解、生成练习、生成观察记录、保存复盘。第一次闭环已经完成。"
            title = "这一关已经完成复盘"
            text = "你已经留下学习记录。下一步可以回到学习轨迹,选择下一关继续练。"
            action_html = '<a class="btn blue" href="/learn#learning-journey">回到学习轨迹</a>'
            action_note = "这一关已经完成,点这里查看自己的学习记录。"
            points = (
                ("已完成", "目标、练习、观察和复盘都已经串起来。"),
                ("可以回看", "教练拆解仍在下面,以后复盘时可以再回来对照。"),
                ("下一步", "从学习轨迹或下一关推荐继续。"),
            )
        elif executed_count > 0:
            loop_completed = 5
            loop_hint = "已完成:理解概念、选择目标、获得教练拆解、生成练习、生成观察记录。还差 1 步:保存三问复盘。"
            title = "下一步:保存三问复盘"
            text = "你已经生成观察记录。现在不要先看赚亏,先回学习工作台保存复盘。"
            action_html = '<a class="btn blue" href="/learn#learning-review">去复盘</a>'
            action_note = "只需要保存三句话复盘,不用判断短期赚亏。"
            points = (
                ("只答三问", "假设是什么、有没有按计划执行、下次修正什么。"),
                ("不用写长", "每问一句话也可以。"),
                ("完成后", "你的学习轨迹会变成已复盘。"),
            )
        elif pending_count > 0:
            loop_completed = 4
            loop_hint = "已完成:理解概念、选择目标、获得教练拆解、生成练习。还差 2 步:生成观察记录、保存复盘。"
            title = "今日练习已生成"
            text = "下一步回到学习工作台,确认观察材料、练习规模和依据后点“生成观察记录”。"
            action_html = '<a class="btn blue" href="/learn#today-practice">回学习页生成观察记录</a>'
            action_note = "回学习页确认后才会生成模拟观察记录,这里不会自动执行。"
            points = (
                ("还没生成记录", "当前只是待观察计划,没有自动执行。"),
                ("先确认", "看懂观察材料、练习规模和依据再生成。"),
                ("生成观察记录后", "系统会带你用这条记录完成三问复盘。"),
            )
        elif signal_count > 0:
            loop_completed = 3
            loop_hint = "已完成:理解概念、选择目标、获得教练拆解。这个任务有练习记录,先回看状态再决定是否继续。"
            title = "这个任务已经有练习记录"
            text = "可以回学习工作台查看当前状态,或者在下面继续预览新的草稿。"
            action_html = '<a class="btn blue" href="/learn#learning-journey">查看学习轨迹</a>'
            action_note = "先看学习轨迹,确认当前练习卡在哪一步。"
            points = (
                ("有记录", "这个任务已经关联过模拟练习。"),
                ("先回看", "确认当前练习在哪一步。"),
                ("再补充", "需要时再从下面生成更多草稿。"),
            )
        else:
            loop_completed = 3
            loop_hint = "已完成:理解概念、选择目标、获得教练拆解。还差 3 步:生成练习、生成观察记录、保存复盘。"
            if task_template == "risk_review":
                title = "下一步只做一件事:一键生成风险练习"
                text = "先不要研究复杂指标。点蓝色按钮后会回到学习工作台,只用它练习写清楚数量、回撤、停止条件。"
                button_label = "一键生成风险练习"
                action_note = "第一次只按这个按钮,不用展开进阶设置;会回到学习工作台继续,不会自动成交。"
                points = (
                    ("数量边界", "每个对象只用小数量,不临时加仓。"),
                    ("回撤边界", "先写下亏损扩大时要观察什么。"),
                    ("停止条件", "看不懂依据或开始情绪化操作,就停止练习。"),
                )
            else:
                title = "刚完成 3/6:点蓝色按钮进入下一步"
                text = f"你已经选了目标,也拿到了{coach_label}拆解。现在不用读完所有说明,先一键生成今日练习。"
                button_label = "一键生成今日练习"
                action_note = "第一次只按这个按钮,不用看完下面长文,不用展开进阶草稿;会回到学习工作台继续,不会自动成交。"
                points = (
                    ("已完成 1", "你已经从蓝色推荐按钮或入门问题选了一个学习目标。"),
                    ("已完成 2", f"系统已经给出{coach_label}拆解,不是空白开始。"),
                    ("下一步", "点击按钮只会保存待观察练习,并回到学习工作台继续。"),
                )
            action_html = (
                f'<form method="post" action="/learn/tasks/{task_id}/quick-save">'
                f"{csrf_input(user)}"
                f'<button class="blue" type="submit">{escape(button_label)}</button>'
                "</form>"
            )
        points_html = "".join(
            f"<div><b>{escape(title_text)}</b><p>{escape(body_text)}</p></div>"
            for title_text, body_text in points
        )
        if reflection_count > 0:
            shortcut_title = "10 秒回看这一关"
            shortcut_text = "这一关已经完成。现在不用继续点交易相关按钮,先回学习轨迹看自己留下的三句话;今天可以停在这里。"
            shortcut_steps = (
                ("已完成", "目标、练习、观察记录、复盘都已经串起来。"),
                ("今天可停", "第一圈已经达标,不需要马上开新题。"),
                ("下次再来", "从学习轨迹里的“下次改什么”继续。"),
            )
        elif executed_count > 0:
            shortcut_title = "10 秒读懂这一页"
            shortcut_text = "预计 30 秒完成。现在不用判断赚亏,也不用读完教练全文。只回学习页保存三问复盘,看到 6/6 就完成。"
            shortcut_steps = (
                ("不用看涨跌", "这一步练的是复盘动作,不是预测对错。"),
                ("只答三问", "想练什么、有没有按规则做、下次改哪一点。"),
                ("完成标志", "保存后会出现 6/6 和学习徽章。"),
            )
        elif pending_count > 0:
            shortcut_title = "10 秒读懂这一页"
            shortcut_text = "预计 60 秒完成。今日练习已经生成。不要再保存新草稿,先回学习页确认最上面那条练习并生成观察记录。"
            shortcut_steps = (
                ("不用展开进阶设置", "第一次只用系统生成的 1 条小练习。"),
                ("回学习页", "确认观察材料、练习规模和依据。"),
                ("下一屏", "生成观察记录后会进入三问复盘。"),
            )
        elif signal_count > 0:
            shortcut_title = "10 秒读懂这一页"
            shortcut_text = "预计 30 秒定位当前步骤。这个任务已经有练习记录。先回学习轨迹确认当前卡在哪一步,不要重复新建目标。"
            shortcut_steps = (
                ("先看轨迹", "确认它是待观察、已观察还是已复盘。"),
                ("再决定", "缺哪一步就补哪一步。"),
                ("别分散", "第一圈完成前不要同时开太多题。"),
            )
        else:
            shortcut_title = "10 秒读懂这一页"
            shortcut_text = "预计 30 秒完成。第一遍不用读完整教练拆解,也不用展开进阶草稿。只确认蓝色按钮会生成今日练习,然后回到学习页继续。"
            shortcut_steps = (
                ("只点蓝色按钮", "一键生成今日练习。"),
                ("不会自动成交", "只是保存一条待观察练习。"),
                ("下一屏", "回到学习工作台看“今日练习”。"),
            )
        shortcut_html = "".join(
            f"<div><strong>{escape(step_title)}</strong><small>{escape(step_text)}</small></div>"
            for step_title, step_text in shortcut_steps
        )
        if reflection_count > 0:
            flow_state = ("done", "done", "done")
        elif executed_count > 0:
            flow_state = ("done", "done", "current")
        elif pending_count > 0 or signal_count > 0:
            flow_state = ("done", "current", "todo")
        else:
            flow_state = ("current", "todo", "todo")
        flow_steps = (
            ("1", "生成练习", "只保存待观察计划,不自动成交。"),
            ("2", "生成观察记录", "回学习工作台确认后,生成一条模拟观察记录。"),
            ("3", "保存复盘", "用示例复盘或三句话完成闭环。"),
        )
        flow_html = "".join(
            '<div class="task-flow-step {klass}">'
            "<span>{status} · {number}/3</span>"
            "<b>{title}</b>"
            "<p>{text}</p>"
            "</div>".format(
                klass=escape(klass),
                status="DONE" if klass == "done" else "NOW" if klass == "current" else "TODO",
                number=escape(number),
                title=escape(step_title),
                text=escape(step_text),
            )
            for klass, (number, step_title, step_text) in zip(flow_state, flow_steps, strict=True)
        )
        return f"""
<section class="card task-action-card" id="task-next-action">
  <div class="task-action-head task-first-action-head">
    <div>
      <span class="tag">NEXT ACTION</span>
      <strong>{escape(title)}</strong>
      <p>{escape(text)}</p>
    </div>
    <div class="next-action-cta">
      {action_html}
      <p class="muted">{escape(action_note)}</p>
    </div>
  </div>
  <div class="task-loop-hint"><b>第一次闭环进度: {loop_completed}/6</b><span>{escape(loop_hint)}</span></div>
  <div class="task-shortcut" id="task-shortcut"><b>{escape(shortcut_title)}</b><span>{escape(shortcut_text)}</span><div class="task-shortcut-grid">{shortcut_html}</div></div>
  <div class="task-flow" aria-label="接下来 3 步">{flow_html}</div>
  <div class="task-action-points">{points_html}</div>
</section>
"""

    def learning_risk_boundary_html(self, task) -> str:
        if services.normalize_learning_template(task["template"] or "reversal") != "risk_review":
            return ""
        return """
<section class="card risk-boundary-card" id="risk-boundary">
  <div class="risk-boundary-head">
    <div>
      <span class="tag">RISK BOUNDARY</span>
      <h2>第三关只看 3 个风险边界</h2>
      <p>风险不是抽象词。第一次只写清楚数量、回撤、停止条件。</p>
    </div>
  </div>
  <div class="risk-boundary-grid">
    <div><b>数量边界</b><p>每个对象只用小数量,不要临时加仓。</p></div>
    <div><b>回撤边界</b><p>先写下如果模拟亏损扩大,你要观察什么,不是马上追涨杀跌。</p></div>
    <div><b>停止条件</b><p>如果看不懂依据、连续偏离原本想练的规则或情绪化操作,就停止这次练习。</p></div>
  </div>
  <div class="risk-boundary-check">
    <strong>生成练习前先记住</strong>
    <p>系统仍会用一条小数量观察作为材料;这一关的重点是给它补上风险边界。</p>
  </div>
</section>
"""

    def render_learning_task_page(
        self,
        user,
        task,
        query,
        preview_rows: list[dict] | None = None,
        preview_error: str = "",
        preview_params: dict | None = None,
    ):
        preview_params = preview_params or {}
        template = services.normalize_learning_template(preview_params.get("template") or self.learning_task_practice_template(task))
        qty = escape(str(preview_params.get("qty") or "100"))
        limit = escape(str(preview_params.get("limit") or "3"))
        strategy_name = escape(str(preview_params.get("strategy_name") or f"学习任务 {int(task['id'])} · {services.LEARNING_TEMPLATES[template]}"))
        rationale_note = escape(str(preview_params.get("rationale_note") or "按 AI 教练建议做小仓位观察,记录想练什么、风险边界和复盘问题。"))
        coach_text = render_markdown(task["coach_text"] or "AI 教练暂无输出。")
        saved_count = services.learning_task_signal_count(self.con, user["id"], int(task["id"]))
        coach_digest_html = self.learning_task_digest_html(task, saved_count)
        task_status_label = {
            "draft": "待生成今日练习",
            "active": "学习中",
            "completed": "已完成",
        }.get(str(task["status"] or ""), str(task["status"] or "学习中"))
        bridge_html = self.learning_task_bridge_html(user, task)
        next_action_html = self.learning_task_next_action_html(user, task)
        risk_boundary_html = self.learning_risk_boundary_html(task)
        mobile_next_bar_html = self.learning_task_mobile_next_bar_html(user, task)
        preview_html = ""
        if preview_error:
            preview_html = f'<div class="msg err">{escape(preview_error)}</div>'
        elif preview_rows is not None:
            rows_html = self.learning_preview_rows_html(preview_rows) or labeled_empty_row("暂无候选", 5)
            preview_html = f"""
<div class="msg">以下只是草稿预览,尚未写入模拟盘。确认后会保存到学习工作台的今日练习;“观察动作”不是买卖指令。</div>
<table class="learning-mobile-table"><thead><tr><th>代码</th><th>名称</th><th>观察动作</th><th>模拟观察数量</th><th>依据</th></tr></thead><tbody>{rows_html}</tbody></table>
"""
        advanced_open = " open" if preview_error or preview_rows is not None else ""
        body = f"""
{self.message_html(query)}
{bridge_html}
{next_action_html}
{risk_boundary_html}
<section class="card">
  <h2>这一关的学习目标</h2>
  <p><span class="badge">{escape(services.LEARNING_DIFFICULTIES.get(task['difficulty'], task['difficulty']))}</span> <span class="badge">{escape(services.LEARNING_TEMPLATES.get(task['template'], task['template']))}</span> <span class="badge">{escape(task_status_label)}</span> <span class="badge">任务编号 {int(task['id'])}</span></p>
  <h3>目标</h3>
  <p>{escape(task['goal'])}</p>
		  {coach_digest_html}
		  <div class="task-action-points">
		    <div><b>教练拆解怎么看</b><p>第一次不用逐字读完,先找“这次学什么”“边界是什么”“下一步做什么”。</p></div>
		    <div><b>看不懂也能继续</b><p>先按上方按钮一键生成今日练习,后面的复盘会帮你把内容说清楚。</p></div>
	    <div><b>始终不是投资建议</b><p>这里讲的是学习方法和模拟流程,不会替你决定现实买卖。</p></div>
	  </div>
	  <details class="advanced-practice coach-breakdown">
	    <summary>可选:展开教练拆解全文<span>第一次先按上方按钮一键生成今日练习;需要理解依据时再展开阅读。</span></summary>
	    <div class="advanced-practice-body">
	      <div class="markdown-body">{coach_text}</div>
	    </div>
	  </details>
	  <p class="muted">已从该任务保存 {saved_count} 条练习。保存后会出现在学习工作台的「今日练习」里,由你自己决定是否生成观察记录。</p>
	</section>
	<details class="card advanced-practice task-advanced-settings"{advanced_open}>
	  <summary>可选:进阶草稿设置<span>第一次不用展开;上方“一键生成今日练习”已经够用。这里适合已经看懂流程后,再预览候选、调整数量和保存更多草稿。</span></summary>
	  <div class="advanced-practice-body">
	    <div class="msg">
	      <strong>第一次可以跳过这里:</strong> 上方已经有“一键生成今日练习”按钮。这里只给已经看懂流程的用户预览候选、调整数量和保存更多草稿。
	    </div>
	    <p><a class="btn blue" href="#task-next-action">回到上方生成按钮</a> <a class="btn secondary" href="/learn">返回学习工作台</a></p>
	      <form method="post" action="/learn/tasks/{int(task['id'])}/preview">
	        {csrf_input(user)}
	        <div class="formline">
          <div><label>练习模板</label><select name="template">{self.learning_template_options(template, include_risk=False)}</select></div>
          <div><label>数量/标的</label><input name="qty" type="number" min="100" step="100" value="{qty}"></div>
          <div><label>候选数</label><input name="limit" type="number" min="1" max="10" step="1" value="{limit}"></div>
          <button type="submit">预览草稿</button>
        </div>
        <label>策略名称</label>
        <input name="strategy_name" value="{strategy_name}" maxlength="80">
        <label>附加记录要求</label>
        <input name="rationale_note" value="{rationale_note}" maxlength="300">
      </form>
	      {preview_html}
	      <form method="post" action="/learn/tasks/{int(task['id'])}/save-signals">
	        {csrf_input(user)}
        <input type="hidden" name="template" value="{escape(template)}">
        <input type="hidden" name="qty" value="{qty}">
	        <input type="hidden" name="limit" value="{limit}">
	        <input type="hidden" name="strategy_name" value="{strategy_name}">
	        <input type="hidden" name="rationale_note" value="{rationale_note}">
	        <p class="muted">还没保存草稿时不要跳到高级模拟盘;先保存到今日练习,再回学习页生成观察记录。高级模拟盘入口会在完成 6/6 后出现。</p>
	        <p><button type="submit">保存当前草稿到今日练习</button> <a class="btn secondary" href="/learn">返回学习工作台</a></p>
	      </form>
		  </div>
		</details>
        {mobile_next_bar_html}
		"""
        self.send_html("学习任务", body, user=user)

    def render_learning_task(self, user, path, query):
        try:
            task_id = self.learning_task_id_from_path(path)
        except ValueError:
            self.not_found()
            return
        task = services.learning_task(self.con, user["id"], task_id)
        if task is None:
            self.not_found()
            return
        self.render_learning_task_page(user, task, query)

    def handle_learning_coach(self, user, form):
        if not self.require_user_write_limit(user, "learning_coach", 8, 3600, "/learn"):
            return
        difficulty = services.normalize_learning_difficulty(form.get("difficulty", "beginner"))
        template = services.normalize_learning_template(form.get("template", "reversal"))
        result = ai_service.coach_learning_goal(
            self.con,
            user["id"],
            secret=_runtime_secret(),
            leak_check_secrets=self.sensitive_secret_values(),
            goal=form.get("goal", ""),
            difficulty=difficulty,
            template=template,
        )
        self.audit(
            "ai.learning_coach",
            user=user,
            target_type="ai",
            detail={"ok": result["ok"], "blocked": result.get("blocked", False), "error": result.get("error", "")},
        )
        if not result["ok"]:
            if result.get("error") == "empty_goal":
                self.redirect("/learn?err=" + quote(result["text"]))
                return
            try:
                task_id = services.create_learning_task(
                    self.con,
                    user["id"],
                    form.get("goal", ""),
                    difficulty,
                    template,
                    self.fallback_learning_coach_text(form.get("goal", ""), difficulty, template, result["text"]),
                )
            except ValueError as exc:
                self.redirect("/learn?err=" + quote(str(exc)))
                return
            self.audit(
                "learning.task_create_fallback",
                user=user,
                target_type="learning_task",
                target_id=task_id,
                detail={"template": template, "ai_error": result.get("error", "")},
            )
            message = "AI 教练暂时不可用,已改用示例教练任务。下一步点击“一键生成今日练习”。"
            self.redirect(f"/learn/tasks/{task_id}?msg=" + quote(message))
            return
        try:
            task_id = services.create_learning_task(
                self.con,
                user["id"],
                form.get("goal", ""),
                difficulty,
                template,
                result["text"],
            )
        except ValueError as exc:
            self.redirect("/learn?err=" + quote(str(exc)))
            return
        self.audit("learning.task_create", user=user, target_type="learning_task", target_id=task_id, detail={"template": template})
        summary = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM learning_tasks WHERE user_id=?) AS task_count,
                (SELECT COUNT(*) FROM learning_reflections WHERE user_id=?) AS reflection_count
            """,
            (int(user["id"]), int(user["id"])),
        ).fetchone()
        if int(summary["task_count"] or 0) > 1 and int(summary["reflection_count"] or 0) > 0:
            message = "下一关已创建:先看它和上一关有什么不同。"
        else:
            message = "AI 教练已完成目标拆解。下一步点击“一键生成今日练习”。"
        self.redirect(f"/learn/tasks/{task_id}?msg=" + quote(message))

    def handle_learning_reflection_save(self, user, form):
        if not self.require_user_write_limit(user, "learning_reflection.save", 30, 600, "/learn"):
            return
        try:
            signal_id = int(form.get("practice_signal_id", "0") or "0")
            reflection_id = services.save_learning_reflection(
                self.con,
                user["id"],
                signal_id,
                form.get("hypothesis", ""),
                form.get("execution_check", ""),
                form.get("adjustment", ""),
            )
        except Exception as exc:  # noqa: BLE001 - keep beginner-facing validation inline
            self.redirect(self.path_with_notice("/learn#learning-review", "err", str(exc)))
            return
        self.audit(
            "learning.reflection_save",
            user=user,
            target_type="learning_reflection",
            target_id=reflection_id,
            detail={"practice_signal_id": signal_id},
        )
        self.redirect(self.path_with_notice("/learn#learning-review", "msg", "第一次学习闭环完成:复盘已保存,你已经解锁 6/6。"))

    def handle_learning_reflection_quick_save(self, user, form):
        if not self.require_user_write_limit(user, "learning_reflection.quick_save", 30, 600, "/learn"):
            return
        try:
            signal_id = int(form.get("practice_signal_id", "0") or "0")
            signal = next(
                (row for row in services.practice_signals(self.con, user["id"], status="executed", limit=50) if int(row["id"]) == signal_id),
                None,
            )
            if signal is None:
                raise ValueError("请先生成观察记录,再保存复盘。")
            title = signal["name"] or signal["code"]
            task = services.learning_task(self.con, user["id"], int(signal["learning_task_id"] or 0)) if signal["learning_task_id"] is not None else None
            current_template = services.LEARNING_TEMPLATES.get(str(task["template"] or ""), str(task["template"] or "这一关")) if task is not None else "这一关"
            previous = self.learning_previous_reflection_row(user, int(signal["learning_task_id"] or 0)) if signal["learning_task_id"] is not None else None
            if previous is not None:
                previous_template = services.LEARNING_TEMPLATES.get(str(previous["template"] or ""), str(previous["template"] or "上一关"))
                previous_adjustment = " ".join(str(previous["adjustment"] or "上一关提醒我要先比较依据、数量和风险边界。").split())[:140]
                hypothesis = f"示例:我这次想练的是把 {current_template} 和上一关 {previous_template} 做对照,看看不同观察角度怎样产生候选。"
                execution_check = f"示例:我仍然只用 {int(signal['qty'])} 的模拟数量,先检查数量、对象和风险边界是否能和上一关保持可比较。"
                adjustment = f"示例:上次提醒自己“{previous_adjustment}”。这次我要看它是否帮助我避免只看涨跌,而是比较候选来源和边界。"
            else:
                hypothesis = f"示例:我这次想练的是把 {title} 当作观察材料,学习一个想法怎样被记录,不是预测它一定上涨。"
                execution_check = f"示例:我按计划只观察 {int(signal['qty'])} 的模拟数量,没有把它当作现实买卖建议。"
                adjustment = "示例:下次我先比较依据、数量和风险边界,再决定是否生成观察记录。"
            reflection_id = services.save_learning_reflection(
                self.con,
                user["id"],
                signal_id,
                hypothesis,
                execution_check,
                adjustment,
            )
        except Exception as exc:  # noqa: BLE001 - beginner-facing validation
            self.redirect(self.path_with_notice("/learn#learning-review", "err", str(exc)))
            return
        self.audit(
            "learning.reflection_quick_save",
            user=user,
            target_type="learning_reflection",
            target_id=reflection_id,
            detail={"practice_signal_id": signal_id},
        )
        self.redirect(self.path_with_notice("/learn#learning-review", "msg", "第一次学习闭环完成:示例复盘已保存,你已经解锁 6/6。"))

    def handle_learning_task_preview(self, user, path, form):
        try:
            task_id = self.learning_task_id_from_path(path)
        except ValueError:
            self.not_found()
            return
        task = services.learning_task(self.con, user["id"], task_id)
        if task is None:
            self.not_found()
            return
        params = {
            "template": form.get("template", task["template"]),
            "qty": form.get("qty", "100"),
            "limit": form.get("limit", "3"),
            "strategy_name": form.get("strategy_name", ""),
            "rationale_note": form.get("rationale_note", ""),
        }
        try:
            rows = services.learning_template_rows(
                self.con,
                user["id"],
                params["template"],
                qty=params["qty"],
                limit=int(params["limit"] or "3"),
            )
        except Exception as exc:  # noqa: BLE001 - show preview errors inline
            self.render_learning_task_page(user, task, parse_qs(""), preview_error=str(exc), preview_params=params)
            return
        self.render_learning_task_page(user, task, parse_qs(""), preview_rows=rows, preview_params=params)

    def handle_learning_task_quick_save(self, user, path):
        if not self.require_user_write_limit(user, "learning_task.quick_save", 20, 600, "/learn"):
            return
        task_id = 0
        try:
            task_id = self.learning_task_id_from_path(path)
            task = services.learning_task(self.con, user["id"], task_id)
            if task is None:
                self.not_found()
                return
            template = self.learning_task_practice_template(task)
            count = services.create_practice_signals_from_learning_task(
                self.con,
                user["id"],
                task_id,
                f"学习任务 {task_id} · {services.LEARNING_TEMPLATES[template]}",
                template,
                qty="100",
                limit=1,
                rationale_note="新手一键生成:先保存 1 条模拟练习到今日练习,再生成观察记录并复盘。",
            )
        except Exception as exc:  # noqa: BLE001 - beginner-facing validation
            target = f"/learn/tasks/{task_id}" if task_id else "/learn"
            self.redirect(target + "?err=" + quote(str(exc)))
            return
        self.audit(
            "learning.quick_signal_saved",
            user=user,
            target_type="learning_task",
            target_id=task_id,
            detail={"count": count, "template": template},
        )
        self.redirect("/learn?msg=" + quote("已生成今日练习。下一步点击“生成观察记录”。") + "#today-practice")

    def handle_learning_task_save_signals(self, user, path, form):
        task_id = 0
        try:
            task_id = self.learning_task_id_from_path(path)
            count = services.create_practice_signals_from_learning_task(
                self.con,
                user["id"],
                task_id,
                form.get("strategy_name", ""),
                form.get("template", ""),
                qty=form.get("qty", "100"),
                limit=int(form.get("limit", "3") or "3"),
                rationale_note=form.get("rationale_note", ""),
            )
        except Exception as exc:  # noqa: BLE001
            target = f"/learn/tasks/{task_id}" if task_id else "/learn"
            self.redirect(target + "?err=" + quote(str(exc)))
            return
        self.audit("learning.signals_saved", user=user, target_type="learning_task", target_id=task_id, detail={"count": count, "template": form.get("template", "")})
        self.redirect("/learn?msg=" + quote(f"已保存 {count} 条练习。现在可以在今日练习里生成观察记录。") + "#today-practice")

