# -*- coding: utf-8 -*-
# @Time : 2022/2/8 20:50
# @Author : @qiu-lzsnmb and @Dimlitter
# @File : auto_comment_plus.py

import argparse, uuid
import html
import copy
import logging
import os
import random
import re
import sys
import time
import urllib
from urllib.parse import parse_qs, urlsplit

import jieba  # just for linting
import jieba.analyse
import requests
import yaml
from lxml import etree

import jdspider

# from http2_adapter import Http2Adapter

# constants
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yml")
USER_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.user.yml")
ORDINARY_SLEEP_SEC = 30
SUNBW_SLEEP_SEC = 5
REVIEW_SLEEP_SEC = 10
SERVICE_RATING_SLEEP_SEC = 15
MIN_COMMENT_CHARS = 21
MAX_COMMENT_CHARS = 500
IMAGE_LIST_RETRY_SEC = 10
CURRENT_USER_AGENT = None
ITEMS_WITH_CURRENT_UA = 0
UA_ROTATE_EVERY = 5
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
]


def rotate_user_agent(logger=None, force=False):
    """每处理固定数量的商品后更换一次 UA，单个小批次内保持一致。"""
    global CURRENT_USER_AGENT, ITEMS_WITH_CURRENT_UA
    if force or CURRENT_USER_AGENT is None or ITEMS_WITH_CURRENT_UA >= UA_ROTATE_EVERY:
        choices = [ua for ua in USER_AGENTS if ua != CURRENT_USER_AGENT] or USER_AGENTS
        CURRENT_USER_AGENT = random.choice(choices)
        ITEMS_WITH_CURRENT_UA = 0
    ITEMS_WITH_CURRENT_UA += 1
    headers["User-Agent"] = CURRENT_USER_AGENT
    headers2["User-Agent"] = CURRENT_USER_AGENT
    if logger:
        logger.debug("当前小批次 User-Agent 已设置（每 %d 个商品更换）", UA_ROTATE_EVERY)
    return CURRENT_USER_AGENT


DEFAULT_COMMENT_TEMPLATES = [
    "收到货后检查了一下，$包装完整，外观和商品描述基本一致，整体使用感受不错。",
    "物流速度比较快，$到手没有磕碰，做工和细节都符合这次的购买预期。",
    "第一次购买这款$，目前使用起来比较方便，操作简单，日常使用很顺手。",
    "这次购物体验不错，$质量看着挺好，功能满足日常需要，后续还会继续关注。",
    "商品收到后试用了几天，$整体表现稳定，实际体验和页面介绍基本相符。",
    "包装比较扎实，配送也很及时，$外观简洁大方，使用起来没有明显问题。",
    "对比了几家之后选择了$，目前来看性价比不错，整体效果符合购买预期。",
    "收到商品后感觉不错，$细节做得比较到位，家里人使用起来也觉得方便。",
    "这款$的做工和质感都还可以，使用过程比较顺畅，暂时没有发现明显问题。",
    "发货速度挺快，包装也很仔细，$到手状态良好，整体购物体验令人满意。",
    "$使用起来比较稳定，外观和功能都符合描述，日常使用基本能够满足需要。",
    "之前看了不少评价才下单，收到$之后感觉没有失望，整体质量确实不错。",
]


def comment_length(text):
    """统计中文、字母和数字，不把空格、标点、表情计为有效字数。"""
    return sum(char.isalnum() for char in text)


def normalize_comment_text(text):
    """还原接口返回的 HTML 实体、URL 编码和常见 UTF-8/Latin-1 乱码。"""
    text = str(text)
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    if re.search(r"%[0-9A-Fa-f]{2}", text):
        try:
            decoded = urllib.parse.unquote(text)
            if decoded != text:
                text = decoded
        except Exception:
            pass

    def quality(value):
        cjk = sum("\u3400" <= char <= "\u9fff" for char in value)
        replacement = value.count("�")
        mojibake = sum(value.count(char) for char in "ÃÂæåçèéêëìíîïðñòóôõö÷øùúûüýþ")
        return cjk * 10 - replacement * 20 - mojibake

    best = text
    for encoding in ("latin1", "cp1252"):
        try:
            candidate = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if quality(candidate) > quality(best):
            best = candidate
    return best


def prepare_comment(text):
    text = normalize_comment_text(text)
    text = re.sub(r"\s+", " ", text).strip()[:MAX_COMMENT_CHARS]
    length = comment_length(text)
    if length < MIN_COMMENT_CHARS:
        raise ValueError(f"评论有效字数 {length}，必须至少 {MIN_COMMENT_CHARS} 字；跳过，不重复填充")
    return text


def report_reward(payload, logger):
    """只记录接口明确给出的京豆/积分奖励提示，不把余额或预计奖励当成到账。"""
    messages = []
    def collect(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key.lower() in {
            "message", "msg", "rewardmessage", "rewardmsg", "tip", "tips"
        }:
            messages.append(value)
    collect(payload)
    pattern = r"(?:获得|获取|赠送|奖励|赚取|获赠|到账)[：:\s]*([0-9]+)\s*(京豆|积分)"
    found = []
    for message in messages:
        found.extend(re.findall(pattern, message))
    if found:
        for amount, unit in dict.fromkeys(found):
            logger.info("接口奖励提示：%s %s（是否到账以京东账户明细为准）", amount, unit)
    else:
        logger.info("接口未明确返回本次奖励数量，不能确认获得多少京豆/积分")


def submit_evaluation(url, data, request_headers, opts, label):
    logger = opts["logger"]
    if "content" in data:
        data = dict(data)
        try:
            content = prepare_comment(data["content"])
        except ValueError as exc:
            logger.warning("%s：%s", label, exc)
            return False
        # 恢复原版的提交格式：先将评论 UTF-8 百分号编码，再交给
        # requests 对整个表单编码。京东此接口会在表单解码后再解析 content。
        # 直接把中文传给 requests 会导致服务端把 UTF-8 字节当成单字节文本，
        # 页面显示为 &ccedil;... 一类实体乱码。图片上传仍使用 multipart，互不影响。
        data["content"] = urllib.parse.quote(content, safe="/", encoding="utf-8")
    if opts.get("dry_run"):
        logger.info("%s：预览完成，未提交", label)
        return False
    try:
        response = requests.post(url, headers=request_headers, data=data, timeout=30)
    except requests.RequestException as exc:
        # 请求超时也可能已经提交成功，因此不自动重试写请求。
        logger.warning("%s：请求失败，结果未知，不自动重试：%s", label, exc)
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    success = (
        response.status_code == 200
        and isinstance(payload, dict)
        and payload.get("success") in (True, "true", 1)
    )
    if success:
        logger.info("%s：成功", label)
        report_reward(payload, logger)
    else:
        logger.warning("%s：未确认成功，status=%s", label, response.status_code)
        logger.debug("提交响应: %s", response.text[:1000])
    return success


def product_id_from_url(url):
    match = re.search(r"(?:https?://)?item\.jd\.com/(\d+)\.html", url)
    return match.group(1) if match else None

# logging with styles
# Reference: https://stackoverflow.com/a/384125/12002560
_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

_RESET_SEQ = "\033[0m"
_COLOR_SEQ = "\033[1;%dm"
_BOLD_SEQ = "\033[1m"
_ITALIC_SEQ = "\033[3m"
_UNDERLINED_SEQ = "\033[4m"

_FORMATTER_COLORS = {
    "DEBUG": _COLORS["blue"],
    "INFO": _COLORS["green"],
    "WARNING": _COLORS["yellow"],
    "ERROR": _COLORS["red"],
    "CRITICAL": _COLORS["red"],
}


def format_style_seqs(msg: str, use_style: bool = True):
    if use_style:
        msg = msg.replace("$RESET", _RESET_SEQ)
        msg = msg.replace("$BOLD", _BOLD_SEQ)
        msg = msg.replace("$ITALIC", _ITALIC_SEQ)
        msg = msg.replace("$UNDERLINED", _UNDERLINED_SEQ)
    else:
        msg = msg.replace("$RESET", "")
        msg = msg.replace("$BOLD", "")
        msg = msg.replace("$ITALIC", "")
        msg = msg.replace("$UNDERLINED", "")


class StyleFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, use_style=True):
        logging.Formatter.__init__(self, fmt, datefmt)
        self.use_style = use_style

    def format(self, record):
        rcd = copy.copy(record)
        levelname = rcd.levelname
        if self.use_style and levelname in _FORMATTER_COLORS:
            levelname_with_color = "%s%s%s" % (
                _COLOR_SEQ % (30 + _FORMATTER_COLORS[levelname]),
                levelname,
                _RESET_SEQ,
            )
            rcd.levelname = levelname_with_color
        return logging.Formatter.format(self, rcd)


# 生成随机文件名
def generate_unique_filename():
    # 获取当前时间戳的最后5位
    return f"{uuid.uuid4().hex}.jpg"


# 下载图片
def download_image(img_url, file_name):
    # 京东返回的图片地址通常是 // 开头，也可能已经是完整 URL。
    if img_url.startswith("//"):
        full_url = f"https:{img_url}"
    elif img_url.startswith(("http://", "https://")):
        full_url = img_url
    else:
        full_url = f"https://{img_url.lstrip('/')}"
    try:
        response = requests.get(full_url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.getLogger("comment").warning("下载图片失败: %s", exc)
        return None

    directory = os.path.join(PROJECT_DIR, "img")
    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, file_name)
    try:
        with open(file_path, "wb") as file:
            file.write(response.content)
    except OSError as exc:
        logging.getLogger("comment").warning("保存图片失败: %s", exc)
        cleanup_downloaded_image(file_path, logging.getLogger("comment"))
        return None
    return file_path


def build_uploaded_image_url(raw_response):
    """将上传接口返回的对象路径转为可被晒单页面正常展示的原图地址。

    京东晒单页面会自行在 /shaidan/ 后加 s300x300_ 和 .dpg。
    因此 imgs 不能再带 s645x515_，否则页面会生成
    s300x300_s645x515_jfs/...，CDN 返回 400。
    """
    value = str(raw_response or "").strip().strip('"').strip()
    if value.startswith(("http://", "https://", "//")):
        parsed = urlsplit("https:" + value if value.startswith("//") else value)
        path = parsed.path
        marker = "/shaidan/"
        if marker in path:
            path = path.split(marker, 1)[1]
        else:
            path = path.lstrip("/")
    else:
        path = value.lstrip("/")
        if path.startswith("shaidan/"):
            path = path[len("shaidan/"):]
    # 去掉接口或历史代码可能带上的尺寸前缀；只保留原始 jfs 对象路径。
    path = re.sub(r"^(?:s\d+x\d+_)+", "", path)
    if path.endswith(".dpg"):
        path = path[:-4]
    if not path or not path.startswith("jfs/") or "." not in path:
        raise ValueError(f"上传接口返回了无法识别的图片路径: {value[:200]}")
    return f"//img30.360buyimg.com/shaidan/{path}"


# 上传图片到 JD 接口。返回 response，失败时由调用方决定是否跳过图片。
def upload_image(filename, file_path, session, headers):
    upload_headers = dict(headers)
    upload_headers.update({
        "Origin": "https://club.jd.com",
        "Referer": "https://club.jd.com/myJdcomments/myJdcomment.action?sort=0",
        "X-Requested-With": "XMLHttpRequest",
        # 不要手工设置 Content-Type，让 requests 正确生成 multipart boundary。
    })
    # requests 的 multipart 文件元组第一个值必须是“文件名”，不能是本地路径。
    with open(file_path, "rb") as image_file:
        files = {
            "name": (None, filename),
            "Filedata": (filename, image_file, "image/jpeg"),
        }
        return session.post(
            "https://club.jd.com/myJdcomments/ajaxUploadImage.action",
            headers=upload_headers,
            files=files,
            timeout=30,
        )


def fetch_image_comment_list(url, request_headers, logger):
    """仅使用旧图片接口：失败后等待 10 秒重试一次，仍失败则无图评价。"""
    for attempt in range(2):
        response = None
        try:
            response = requests.get(url, headers=request_headers, timeout=20)
            response.raise_for_status()
            data = response.json()
            comments = data.get("imgComments") if isinstance(data, dict) else None
            if not isinstance(comments, dict):
                raise ValueError("图片评论数据结构不正确")
            image_list = comments.get("imgList")
            if image_list is None and comments.get("imgCommentCount") == 0:
                image_list = []
            if not isinstance(image_list, list):
                raise ValueError("图片列表数据结构不正确")
            if attempt:
                logger.info("图片接口重试成功，获取 %d 张候选图片", len(image_list))
            return image_list
        except (requests.RequestException, ValueError) as exc:
            status = response.status_code if response is not None else "无响应"
            content_type = (response.headers.get("Content-Type", "未提供")
                            if response is not None else "未提供")
            byte_count = len(response.content) if response is not None else 0
            if attempt == 0:
                logger.warning(
                    "获取图片列表异常（%s，status=%s，content_type=%s，bytes=%d）；"
                    "%d 秒后重试当前商品，期间不提交评价。",
                    type(exc).__name__, status, content_type, byte_count,
                    IMAGE_LIST_RETRY_SEC,
                )
                time.sleep(IMAGE_LIST_RETRY_SEC)
            else:
                logger.warning(
                    "图片列表重试仍失败（%s，status=%s，content_type=%s，bytes=%d）；"
                    "切换无图模式，继续提交当前文字评价。",
                    type(exc).__name__, status, content_type, byte_count,
                )
    return []


def fetch_product_comments(product_id, request_headers, limit=30):
    """读取当前订单商品的好评内容，避免依赖已经变化的搜索页 DOM。"""
    url = (
        "https://club.jd.com/discussion/getProductPageImageCommentList.action"
        f"?productId={product_id}"
    )
    try:
        response = requests.get(url, headers=request_headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logging.getLogger("comment").warning("读取商品已有评论失败: %s", exc)
        return []

    result = []
    for item in ((payload.get("imgComments") or {}).get("imgList") or []):
        content = str((item.get("commentVo") or {}).get("content", ""))
        content = normalize_comment_text(content)
        content = re.sub(r"[\r\n]+", " ", content).strip()
        if content:
            result.append(content)
        if len(result) >= limit:
            break
    return result


# 评价生成
def generation(
    pname: str,
    _class: int = 0,
    _type: int = 1,
    opts: object = None,
    product_id: str = None,
):
    result = []
    opts = opts or {}
    items = ["商品名"]
    items.clear()
    items.append(pname)
    opts["logger"].debug("Items: %s", items)
    loop_times = len(items)
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i, item in enumerate(items):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        opts["logger"].debug("Current item: %s", item)
        # 默认不复用京东已有评论。已有评论可能带有 HTML 实体或历史乱码，
        # 直接拼接后再提交会把这些编码原样带到新评价中。默认只使用本地干净模板。
        # 如确实需要测试已有评论素材，可显式传 opts["use_existing_comments"] = True。
        use_existing_comments = bool(opts.get("use_existing_comments", False))
        if product_id and use_existing_comments:
            result = fetch_product_comments(product_id, headers)
            opts["logger"].debug("Current product comments: %d", len(result))
        else:
            result = []
        # 增加对增值服务的评价鉴别
        if not result and ("赠品" in pname or "非实物" in pname or "增值服务" in pname):
            result = [
                "赠品挺好的。",
                "很贴心，能有这样免费赠送的赠品!",
                "正好想着要不要多买一份增值服务，没想到还有这样的赠品。",
                "赠品正合我意。",
                "赠品很好，挺不错的。",
                "本来买了产品以后还有些担心。但是看到赠品以后就放心了。",
                "不论品质如何，至少说明店家对客的态度很好！",
                "我很喜欢这些商品！",
                "我对于商品的附加值很在乎，恰好这些赠品为这件商品提供了这样的的附加值，这令我很满意。"
                "感觉现在的网购环境环境越来越好了，以前网购的时候还没有过么多贴心的赠品和增值服务",
                "第一次用京东，被这种赠品和增值服物的良好态度感动到了。",
                "赠品还行。",
            ]
        elif not result and use_existing_comments:
            # 仅在显式开启时使用旧的同类商品搜索逻辑。
            spider = jdspider.JDSpider(item)
            opts["logger"].debug("Successfully created a JDSpider instance")
            result = spider.getData(2, 3)  # 这里可以自己改
        if not result:
            # 默认使用本地干净模板，避免把京东历史评论中的实体编码再次提交。
            result = DEFAULT_COMMENT_TEMPLATES.copy()
        opts["logger"].debug("Result: %s", result)

    # class 0是评价 1是提取id
    try:
        name = jieba.analyse.textrank(pname, topK=5, allowPOS="n")[0]
        opts["logger"].debug("Name: %s", name)
    except Exception as e:
        opts["logger"].warning(
            'jieba textrank analysis error: %s, name fallback to "宝贝"', e
        )
        name = "宝贝"
    if _class == 1:
        opts["logger"].debug("_class is 1. Directly return name")
        return name
    else:
        num = 0
        if _type == 1:
            num = 6
        elif _type == 0:
            num = 4
        num = min(num, len(result))
        # use `.join()` to improve efficiency
        pieces = [str(item).replace("$", name).strip() for item in result if item]
        pieces = list(dict.fromkeys(pieces))
        random.shuffle(pieces)
        comments = "".join(pieces[:num])
        # 从剩余素材补足，不用重复同一句话或虚构填充文本凑字数。
        for piece in pieces[num:]:
            if comment_length(comments[:MAX_COMMENT_CHARS]) >= MIN_COMMENT_CHARS:
                break
            comments += piece
        comments = prepare_comment(comments)
        opts["logger"].info("评论长度：%d 字符 / %d 有效字（最低 %d）",
                            len(comments), comment_length(comments), MIN_COMMENT_CHARS)
        opts["logger"].debug("_type: %d", _type)
        opts["logger"].debug("num: %d", num)
        opts["logger"].debug("Raw comments: %s", comments)

        return 5, comments


# 查询全部评价
def all_evaluate(opts=None):
    opts = opts or {}
    N = {}
    url = "https://club.jd.com/myJdcomments/myJdcomment.action?"
    opts["logger"].info("URL: %s", url)
    opts["logger"].debug("Fetching website data")
    req = requests.get(url, headers=headers, timeout=20)
    opts["logger"].debug(
        "Successfully accepted the response with status code %d", req.status_code
    )
    if not req.ok:
        opts["logger"].debug(
            "Status code of the response is %d, not 200", req.status_code
        )
    req_et = etree.HTML(req.text)
    opts["logger"].debug("Successfully parsed an XML tree")
    evaluate_data = req_et.xpath('//*[@id="main"]/div[2]/div[1]/div/ul/li')
    # print(evaluate)
    loop_times = len(evaluate_data)
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i, ev in enumerate(evaluate_data):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        na = ev.xpath("a/text()")[0]
        opts["logger"].debug("na: %s", na)
        try:
            num = ev.xpath("b/text()")[0]
            opts["logger"].debug("num: %s", num)
        except IndexError:
            opts["logger"].info("Can't find num content in XPath, fallback to 0")
            num = 0
        N[na] = int(num)
    return N


def cleanup_downloaded_image(file_path, logger):
    """仅删除调用方记录的本次下载文件，不扫描/批量清理其他图片。"""
    try:
        os.remove(file_path)
        logger.debug("已清理本次下载图片：%s", file_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("清理下载图片失败：%s (%s)", file_path, exc)


# 普通评价
def ordinary(N, opts=None):
    time.sleep(3)
    opts = opts or {}
    Order_data = []
    seen = set()
    req_et = []
    loop_times = (N["待评价订单"] + 19) // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times):
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=0&"
            f"page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, timeout=20)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tbody')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["待评价订单"]:
        opts["logger"].debug(
            'Count of fetched order data doesn\'t equal N["待评价订单"]'
        )
        opts["logger"].debug("Clear the list Order_data")
        Order_data = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table')
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            Order_data.extend(elems)

    opts["logger"].info(f"当前共有{N['待评价订单']}个评价。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        try:
            oid = Order.xpath('tr[@class="tr-th"]/td/span[3]/a/text()')[0]
            opts["logger"].debug("oid: %s", oid)
            oname_data = Order.xpath(
                'tr[@class="tr-bd"]/td[1]/div[1]/div[2]/div/a/text()'
            )
            opts["logger"].debug("oname_data: %s", oname_data)
            pid_data = Order.xpath('tr[@class="tr-bd"]/td[1]/div[1]/div[2]/div/a/@href')
            opts["logger"].debug("pid_data: %s", pid_data)
        except IndexError:
            opts["logger"].warning(f"第{i + 1}个订单未查找到商品，跳过。")
            continue
        loop_times1 = min(len(oname_data), len(pid_data))
        opts["logger"].debug("Commenting on orders")
        opts["logger"].debug("Total loop times: %d", loop_times1)
        idx = 0
        for oname, pid in zip(oname_data, pid_data):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times1)
            pid = product_id_from_url(pid)
            if not pid:
                opts["logger"].warning("商品链接无法识别，跳过：%s", oname)
                continue
            key = (str(oid).strip(), pid)
            if key in seen:
                continue
            seen.add(key)
            opts["logger"].debug("pid: %s", pid)
            if "javascript" in pid:
                opts["logger"].error(
                    "pid_data: %s,这个订单估计是京东外卖的，会导致此次评价失败，请把该 %s 商品手工评价后再运行程序。"
                    % (pid, oname),
                )
                continue
            opts["logger"].info(f"\t{i}.开始评价订单\t{oname}[{oid}]并晒图")
            rotate_user_agent(opts["logger"])
            url2 = "https://club.jd.com/myJdcomments/saveProductComment.action"
            opts["logger"].debug("URL: %s", url2)
            try:
                xing, Str = generation(oname, product_id=pid, opts=opts)
            except ValueError as exc:
                opts["logger"].warning("%s：%s", oname, exc)
                continue
            opts["logger"].info(f"\t\t评价内容,星级{xing}：" + Str)
            # 获取已有晒单图片，并逐张上传到当前晒单会话。
            # 每个订单都重新初始化，避免上一单的状态污染下一单。
            uploaded_image_urls = []
            opts["logger"].info("\t\t开始获取图片")
            img_url = (
                f"https://club.jd.com/discussion/getProductPageImageCommentList"
                f".action?productId={pid}"
            )
            opts["logger"].debug("Fetching images using the default URL: %s", img_url)
            image_list = fetch_image_comment_list(img_url, headers, opts["logger"])
            if not image_list:
                opts["logger"].info("这单没有可用图片，继续发送文字评价")
            elif opts.get("dry_run"):
                opts["logger"].info("预览模式：找到 %d 张候选图片，不下载、不上传", len(image_list))
            else:
                with requests.Session() as image_session:
                    for image_index, image_item in enumerate(image_list[:2], start=1):
                        source_url = image_item.get("imageUrl", "")
                        if not source_url:
                            continue
                        image_name = generate_unique_filename()
                        downloaded_file = download_image(source_url, image_name)
                        if not downloaded_file:
                            continue
                        try:
                            image_response = upload_image(
                                image_name, downloaded_file, image_session, headers
                            )
                            body = image_response.text.strip().strip('"')
                            opts["logger"].debug(
                                "第 %d 张图片上传响应: status=%s body=%s",
                                image_index, image_response.status_code, body[:300],
                            )
                            if image_response.ok and body and ".jpg" in body:
                                try:
                                    uploaded_url = build_uploaded_image_url(body)
                                except ValueError as exc:
                                    opts["logger"].warning("上传成功但图片地址无效: %s", exc)
                                else:
                                    opts["logger"].debug("第 %d 张图片提交地址: %s", image_index, uploaded_url)
                                    uploaded_image_urls.append(uploaded_url)
                            else:
                                opts["logger"].warning(
                                    "第 %d 张图片上传失败: status=%s body=%s",
                                    image_index, image_response.status_code, body[:300],
                                )
                        except (OSError, requests.RequestException) as exc:
                            opts["logger"].warning("第 %d 张图片上传异常: %s", image_index, exc)
                        finally:
                            # 上传已读取完文件，提交评价只需要 URL，不再需要本地图片。
                            cleanup_downloaded_image(downloaded_file, opts["logger"])
                if uploaded_image_urls:
                    imgurl = ",".join(uploaded_image_urls)
                    opts["logger"].info("\t\t已上传 %d 张图片", len(uploaded_image_urls))
                else:
                    opts["logger"].warning("图片均上传失败，继续发送无图评价")

            Comment_data = {
                "orderId": oid,
                "productId": pid,  # 商品id
                "score": str(xing),  # 商品几星
                "content": Str,  # 评价内容
                "saveStatus": "1",
                "anonymousFlag": "1",  # 是否匿名
            }
            if uploaded_image_urls:
                Comment_data["imgs"] = imgurl  # 仅提交实际上传成功的图片
            opts["logger"].debug("Data: %s", Comment_data)
            if submit_evaluation(url2, Comment_data, headers2, opts, f"{oname}[{oid}]普通评价"):
                N["待评价订单"] = max(0, N["待评价订单"] - 1)
            if idx < loop_times1 - 1:
                opts["logger"].info("等待 %.0f 秒后评论下一条", ORDINARY_SLEEP_SEC)
            else:
                opts["logger"].info("当前待评价列表处理完成")
            opts["logger"].debug("Sleep time (s): %.1f", ORDINARY_SLEEP_SEC)
            time.sleep(ORDINARY_SLEEP_SEC)
            idx += 1
    return N


"""
# 晒单评价
def sunbw(N, opts=None):
    opts = opts or {}
    Order_data = []
    loop_times = N['待晒单'] // 20
    opts['logger'].debug('Fetching website data')
    opts['logger'].debug('Total loop times: %d', loop_times)
    for i in range(loop_times + 1):
        opts['logger'].debug('Loop: %d / %d', i + 1, loop_times)
        url = (f'https://club.jd.com/myJdcomments/myJdcomment.action?sort=1'
               f'&page={i + 1}')
        opts['logger'].debug('URL: %s', url)
        req = requests.get(url, headers=headers, timeout=20)
        opts['logger'].debug(
            'Successfully accepted the response with status code %d',
            req.status_code)
        if not req.ok:
            opts['logger'].warning(
                'Status code of the response is %d, not 200', req.status_code)
        req_et = etree.HTML(req.text)
        opts['logger'].debug('Successfully parsed an XML tree')
        opts['logger'].debug('Fetching data from XML trees')
        elems = req_et.xpath(
            '//*[@id="evalu01"]/div[2]/div[1]/div[@class="comt-plist"]/div[1]')
        opts['logger'].debug('Count of fetched order data: %d', len(elems))
        Order_data.extend(elems)
    opts['logger'].info(f"当前共有{N['待晒单']}个需要晒单。")
    opts['logger'].debug('Commenting on items')
    for i, Order in enumerate(Order_data):
        oname = Order.xpath('ul/li[1]/div/div[2]/div[1]/a/text()')[0]
        pid = Order.xpath('@pid')[0]
        oid = Order.xpath('@oid')[0]
        opts['logger'].info(f'\t开始第{i+1}，{oname}')
        opts['logger'].debug('pid: %s', pid)
        opts['logger'].debug('oid: %s', oid)
        # 获取图片
        url1 = (f'https://club.jd.com/discussion/getProductPageImageCommentList'
                f'.action?productId={pid}')
        opts['logger'].debug('Fetching images using the default URL')
        opts['logger'].debug('URL: %s', url1)
        req1 = requests.get(url1, headers=headers)
        opts['logger'].debug(
            'Successfully accepted the response with status code %d',
            req1.status_code)
        if not req.ok:
            opts['logger'].warning(
                'Status code of the response is %d, not 200', req1.status_code)
        imgdata = req1.json()
        opts['logger'].debug('Image data: %s', imgdata)
        if imgdata["imgComments"]["imgCommentCount"] == 0:
            opts['logger'].debug('Count of fetched image comments is 0')
            opts['logger'].debug('Fetching images using another URL')
            url1 = ('https://club.jd.com/discussion/getProductPageImage'
                    'CommentList.action?productId=1190881')
            opts['logger'].debug('URL: %s', url1)
            req1 = requests.get(url1, headers=headers)
            opts['logger'].debug(
                'Successfully accepted the response with status code %d',
                req1.status_code)
            if not req.ok:
                opts['logger'].warning(
                    'Status code of the response is %d, not 200',
                    req1.status_code)
            imgdata = req1.json()
            opts['logger'].debug('Image data: %s', imgdata)
        imgurl = imgdata["imgComments"]["imgList"][0]["imageUrl"]
        opts['logger'].debug('Image URL: %s', imgurl)

        opts['logger'].info(f'\t\t图片url={imgurl}')
        # 提交晒单
        opts['logger'].debug('Preparing for commenting')
        url2 = "https://club.jd.com/myJdcomments/saveShowOrder.action"
        opts['logger'].debug('URL: %s', url2)
        headers['Referer'] = ('https://club.jd.com/myJdcomments/myJdcomment.'
                              'action?sort=1')
        headers['Origin'] = 'https://club.jd.com'
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        opts['logger'].debug('New header for this request: %s', headers)
        data = {
            'orderId': oid,
            'productId': pid,
            'imgs': imgurl,
            'saveStatus': 3
        }
        opts['logger'].debug('Data: %s', data)
        if not opts.get('dry_run'):
            opts['logger'].debug('Sending comment request')
            req_url2 = requests.post(url2, data=data, headers=headers)
        else:
            opts['logger'].debug('Skipped sending comment request in dry run')
        opts['logger'].info('完成')
        opts['logger'].debug('Sleep time (s): %.1f', SUNBW_SLEEP_SEC)
        time.sleep(SUNBW_SLEEP_SEC)
        N['待晒单'] -= 1
    return N
"""

# 追评


def review(N, opts=None):
    opts = opts or {}
    req_et = []
    Order_data = []
    seen = set()
    loop_times = (N["待追评"] + 19) // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=3"
            f"&page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, timeout=20)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tr[@class="tr-bd"]')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["待追评"]:
        opts["logger"].debug('Count of fetched order data doesn\'t equal N["待追评"]')
        alternate_orders = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath(
                '//*[@id="main"]/div[2]/div[2]/table/tbody/tr[@class="tr-bd"]'
            )
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            alternate_orders.extend(elems)
        if alternate_orders:
            Order_data = alternate_orders
    opts["logger"].info(f"当前共有 {N['待追评']} 个需要追评。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        oname = Order.xpath("td[1]/div/div[2]/div/a/text()")[0]
        _id = Order.xpath("td[3]/div/a/@href")[0]
        opts["logger"].info(f"\t开始追评第{i+1}，{oname}")
        rotate_user_agent(opts["logger"])
        opts["logger"].debug("_id: %s", _id)
        url1 = (
            "https://club.jd.com/afterComments/" "saveAfterCommentAndShowOrder.action"
        )
        opts["logger"].debug("URL: %s", url1)
        query = parse_qs(urlsplit(_id).query)
        pid = (query.get("sku") or [""])[0]
        oid = (query.get("orderId") or [""])[0]
        if not pid.isdigit() or not oid.isdigit():
            opts["logger"].warning("追评链接无法识别，跳过：%s", oname)
            continue
        key = (oid, pid)
        if key in seen:
            continue
        seen.add(key)
        try:
            _, context = generation(oname, _type=0, product_id=pid, opts=opts)
        except ValueError as exc:
            opts["logger"].warning("%s：%s", oname, exc)
            continue
        opts["logger"].info(f"\t\t追评内容：{context}")
        data1 = {
            "orderId": oid,
            "productId": pid,
            "content": context,
            "anonymousFlag": 1,
            "score": 5,
            "imgs": "",
        }
        opts["logger"].debug("Data: %s", data1)
        if submit_evaluation(url1, data1, headers2, opts, f"{oname}[{oid}]追评"):
            N["待追评"] = max(0, N["待追评"] - 1)
        opts["logger"].debug("Sleep time (s): %.1f", REVIEW_SLEEP_SEC)
        time.sleep(REVIEW_SLEEP_SEC)
    return N


# 服务评价
def Service_rating(N, opts=None):
    opts = opts or {}
    Order_data = []
    seen = set()
    req_et = []
    loop_times = (N["服务评价"] + 19) // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=4"
            f"&page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, timeout=20)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tbody/tr[@class="tr-bd"]')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["服务评价"]:
        opts["logger"].debug('Count of fetched order data doesn\'t equal N["服务评价"]')
        opts["logger"].debug("Clear the list Order_data")
        Order_data = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tr[@class="tr-bd"]')
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            Order_data.extend(elems)
    opts["logger"].info(f"当前共有{N['服务评价']}个需要第一次服务评价。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        oname = Order.xpath("td[1]/div[1]/div[2]/div/a/text()")[0]
        try:
            oid = Order.xpath("td[4]/div/a[1]/@oid")[0]
        except IndexError:
            opts["logger"].warning("Failed to fetch oid")
            continue
        opts["logger"].info(f"\t开始第一次评论，{i+1}，{oname}")
        rotate_user_agent(opts["logger"])
        opts["logger"].debug("oid: %s", oid)
        url1 = (
            f"https://club.jd.com/myJdcomments/insertRestSurvey.action"
            f"?voteid=145&ruleid={oid}"
        )
        opts["logger"].debug("URL: %s", url1)
        data1 = {
            "oid": oid,
            "gid": "32",
            "sid": "186194",
            "stid": "0",
            "tags": "",
            "ro591": f"591A{random.randint(4, 5)}",  # 商品符合度
            "ro592": f"592A{random.randint(4, 5)}",  # 店家服务态度
            "ro593": f"593A{random.randint(4, 5)}",  # 快递配送速度
            "ro899": f"899A{random.randint(4, 5)}",  # 快递员服务
            "ro900": f"900A{random.randint(4, 5)}",  # 快递员服务
        }
        opts["logger"].debug("Data: %s", data1)
        if oid in seen:
            continue
        seen.add(oid)
        if submit_evaluation(url1, data1, headers, opts, f"{oname}[{oid}]服务评价"):
            N["服务评价"] = max(0, N["服务评价"] - 1)
        opts["logger"].debug("Sleep time (s): %.1f", SERVICE_RATING_SLEEP_SEC)
        time.sleep(SERVICE_RATING_SLEEP_SEC)
    return N


def No(opts=None):
    opts = opts or {}
    # opts["logger"].info("")
    N = all_evaluate(opts)
    s = "----".join(["{} {}".format(i, N[i]) for i in N])
    opts["logger"].info(s)
    # opts["logger"].info("")
    return N


def main(opts=None):
    opts = opts or {}
    logger = opts["logger"]
    logger.info("开始京东批量评价（单轮，不自动重试）")
    N = No(opts)
    if not N:
        logger.error("无法读取评价列表，请检查 Cookie 或页面结构")
        return
    logger.info("已评价：%s 个", N.get("已评价", "未知"))
    for key, handler in (
        ("待评价订单", ordinary),
        ("待追评", review),
        ("服务评价", Service_rating),
    ):
        count = N.get(key)
        if count is None:
            logger.warning("页面缺少 %s 数量，跳过该阶段", key)
            continue
        if count <= 0:
            continue
        logger.info("开始%s，共 %d 个", key, count)
        try:
            N = handler(dict(N), opts)
        except (requests.RequestException, ValueError, IndexError, AttributeError) as exc:
            logger.warning("%s处理异常，停止该阶段，不自动重试：%s", key, exc)
        if not opts.get("dry_run"):
            latest = No(opts)
            if not latest:
                logger.warning("无法刷新评价列表，停止本轮")
                return
            N = latest
    if opts.get("dry_run"):
        logger.info("本轮预览完成：未上传图片、未提交评价，实际待处理数量未改变")
        return
    remaining = {key: N[key] for key in ("待评价订单", "待追评", "服务评价")
                 if N.get(key, 0) > 0}
    if remaining:
        logger.warning("本轮结束，仍有待处理项目：%s；请核查，不自动重试", remaining)
    else:
        logger.info("本轮结束，未发现剩余待处理项目（未读取到的分类不作成功判断）")


if __name__ == "__main__":
    # parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        help="have a full run without comment submission",
        action="store_true",
    )
    parser.add_argument(
        "-lv",
        "--log-level",
        help="specify logging level (default: info)",
        default="INFO",
    )
    parser.add_argument(
        "-o", "--log-file", help="specify logging file", default="log.txt"
    )
    args = parser.parse_args()
    if args.log_level.upper() not in [
        "DEBUG",
        "WARN",
        "INFO",
        "ERROR",
        "FATAL",
        # NOTE: `WARN` is an alias of `WARNING`. `FATAL` is an alias of
        # `CRITICAL`. Using these aliases is for developers' and users'
        # convenience.
        # NOTE: Now there is no logging on `CRITICAL` level.
    ]:
        args.log_level = "INFO"
    else:
        args.log_level = args.log_level.upper()
    opts = {"dry_run": args.dry_run, "log_level": args.log_level}
    if hasattr(args, "log_file"):
        opts["log_file"] = args.log_file
    else:
        opts["log_file"] = None

    # logging on console
    _logging_level = getattr(logging, opts["log_level"])
    logger = logging.getLogger("comment")
    logger.setLevel(level=_logging_level)
    # NOTE: `%(levelname)s` will be parsed as the original name (`FATAL` ->
    # `CRITICAL`, `WARN` -> `WARNING`).
    # NOTE: The alignment number should set to 19 considering the style
    # controling characters. When it comes to file logger, the number should
    # set to 8.
    formatter = StyleFormatter("%(asctime)s %(levelname)-19s %(message)s")
    rawformatter = StyleFormatter(
        "%(asctime)s %(levelname)-8s %(message)s", use_style=False
    )
    console = logging.StreamHandler()
    console.setLevel(_logging_level)
    console.setFormatter(formatter)
    logger.addHandler(console)
    opts["logger"] = logger
    # It's a hack!!!
    jieba.default_logger = logging.getLogger("jieba")
    jieba.default_logger.setLevel(level=_logging_level)
    jieba.default_logger.addHandler(console)
    # It's another hack!!!
    jdspider.default_logger = logging.getLogger("spider")
    jdspider.default_logger.setLevel(level=_logging_level)
    jdspider.default_logger.addHandler(console)

    logger.debug("Successfully set up console logger")
    logger.debug("CLI arguments: %s", args)
    logger.debug("Opening the log file")
    if opts["log_file"]:
        try:
            handler = logging.FileHandler(opts["log_file"], "w")
        except Exception as e:
            logger.error("Failed to open the file handler")
            logger.error("Error message: %s", e)
            sys.exit(1)
        handler.setLevel(_logging_level)
        handler.setFormatter(rawformatter)
        logger.addHandler(handler)
        jieba.default_logger.addHandler(handler)
        jdspider.default_logger.addHandler(handler)
        logger.debug("Successfully set up file logger")
    logger.debug("Options passed to functions: %s", opts)
    logger.debug("Builtin constants:")
    logger.debug("  CONFIG_PATH: %s", CONFIG_PATH)
    logger.debug("  USER_CONFIG_PATH: %s", USER_CONFIG_PATH)
    logger.debug("  ORDINARY_SLEEP_SEC: %s", ORDINARY_SLEEP_SEC)
    logger.debug("  UA_ROTATE_EVERY: %s", UA_ROTATE_EVERY)
    logger.debug("  SUNBW_SLEEP_SEC: %s", SUNBW_SLEEP_SEC)
    logger.debug("  REVIEW_SLEEP_SEC: %s", REVIEW_SLEEP_SEC)
    logger.debug("  SERVICE_RATING_SLEEP_SEC: %s", SERVICE_RATING_SLEEP_SEC)

    # parse configurations
    logger.debug("Reading the configuration file")
    if os.path.exists(USER_CONFIG_PATH):
        logger.debug("User configuration file exists")
        _cfg_path = USER_CONFIG_PATH
    else:
        logger.debug(
            "User configuration file doesn't exist, fallback to the default one"
        )
        _cfg_path = CONFIG_PATH
    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.debug("Closed the configuration file")
    logger.debug("配置已加载，不输出 Cookie")
    ck = str((cfg.get("user") or {}).get("cookie", "")).strip().strip("'\"")
    if not ck:
        logger.error("Cookie 未配置，请填写 config.user.yml")
        sys.exit(1)
    jdspider.cookie = ck

    headers2 = {
        "Cookie": ck,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.5735.110 Safari/537.36",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": "",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://club.jd.com/",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 'Content-Type':'application/x-www-form-urlencoded'
    }
    headers = {
        "Cookie": ck,
        "User-Agent": '''Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0 Sec-Ch-Ua: "Chromium";v="136", "Microsoft Edge";v="136", "Not.A/Brand";v="99"''',
        "DNT": "1",
        # "Connection": "keep-alive",
        # "Cache-Control": "max-age=0",
        # "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="98", "Google Chrome";v="98"',
        # "sec-ch-ua-mobile": "?0",
        # "sec-ch-ua-platform": '"Windows"',
        # "Upgrade-Insecure-Requests": "1",
        # "Accept": "*/*",
        # "Sec-Fetch-Site": "same-site",
        # "Sec-Fetch-Mode": "navigate",
        # "origin": "https://club.jd.com",
        # "Sec-Fetch-User": "?1",
        # "Sec-Fetch-Dest": "document",
        # "Referer": "https://order.jd.com/",
        # "Accept-Encoding": "gzip, deflate, br, zstd",
        # "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    }
    logger.debug("HTTP 请求头已配置，不输出 Cookie")
    # 随机一次即可；每个商品更换 UA 反而会造成设备指纹不一致，更容易触发风控。
    rotate_user_agent(logger, force=True)

    logger.debug("Starting main processes")
    try:
        main(opts)
    except (requests.RequestException, ValueError, IndexError, AttributeError) as exc:
        logger.error("运行中止，请检查接口或配置：%s", exc)
