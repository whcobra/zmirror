#!/usr/bin/env python3
# coding=utf-8
import os
import re
import copy
import zlib
import sched
import queue
import pickle
import base64
import random
import traceback
import ipaddress
import threading

from fnmatch import fnmatch
from time import time, sleep, process_time
from html import escape as html_escape
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit, quote_plus
import urllib.parse
import requests
from flask import Flask, request, make_response, Response, redirect
from . import CONSTS

try:
    # for python 3.5+ Type Hint
    from typing import Union, List, Any, Tuple
except:
    pass
try:  # 用于检测html的文本编码, cchardet是chardet的c语言实现, 非常快
    from cchardet import detect as c_chardet
except:
    cchardet_available = False
else:
    cchardet_available = True

if os.path.abspath(os.getcwd()) != CONSTS.ZMIRROR_ROOT:
    os.chdir(CONSTS.ZMIRROR_ROOT)

from .external_pkgs.ColorfulPyPrint import *  # TODO: Migrate logging tools to the stdlib logging

if "ZMIRROR_UNITTEST" in os.environ:
    # 这边根据环境变量得到的unittest_mode信息会被config中的覆盖掉
    # 只是因为此时还没有加载 config, 所以先根据env里的临时定一下
    unittest_mode = True
else:
    unittest_mode = False

try:  # lru_cache的c语言实现, 比Python内置lru_cache更快
    from fastcache import lru_cache  # lru_cache用于缓存函数的执行结果
except:
    from functools import lru_cache

    warnprint('package fastcache not found, '
              'fallback to stdlib lru_cache, '
              'no FUNCTION is effected, only maybe a bit slower. '
              'Considering install it using "pip3 install fastcache"'
              )
else:
    if not unittest_mode:
        infoprint('lru_cache loaded successfully from fastcache')

from .threadlocal import ZmirrorThreadLocal

if not unittest_mode:  # 在unittest时不输出这几行
    infoprint('zmirror version: {version} author: {author}'.format(version=CONSTS.__VERSION__, author=CONSTS.__AUTHOR__))
    infoprint('Github: {site_url}'.format(site_url=CONSTS.__GITHUB_URL__))

try:  # 加载默认设置
    from config_default import *
except:
    errprint('the config_default.py is missing, this program may not works normally\n'
             'config_default.py 文件丢失, 这会导致配置文件不向后兼容, 请重新下载一份 config_default.py')
    raise  # v0.23.1+ 当 config_default.py 不存在时, 程序会终止运行

try:  # 加载用户自定义配置文件, 覆盖掉默认配置的同名项
    from config import *
except:
    errprint(
        'the config_default.py is missing, fallback to default configs(if we can), '
        'please COPY the config_default.py to config.py, and change it\'s content, '
        'or use the configs in the more_configs folder\n'
        '自定义配置文件 config.py 丢失或存在错误, 将使用默认设置, 请将 config_default.py 复制一份为 config.py, '
        '并根据自己的需求修改里面的设置'
        '(或者使用 more_configs 中的配置文件)'
    )
    raise  # v0.23.1+ 当config文件存在错误或不存在时, 程序会终止运行
else:
    target_domain = target_domain.strip("./ \t").replace("https://", "").replace("http://", "")
    infoprint('config file found, mirroring: ', target_domain)

if unittest_mode:
    import importlib

    importlib.reload(importlib.import_module("zmirror.utils"))
from .utils import *

if local_cache_enable:
    try:
        from .cache_system import FileCache, get_expire_from_mime

        cache = FileCache()
    except Exception as e:
        traceback.print_exc()
        errprint('Can Not Create Local File Cache: ', e, ' local file cache is disabled automatically.')
        local_cache_enable = False
    else:
        if not unittest_mode:
            infoprint('Local file cache enabled')

# ########## Basic Init #############
# 开始从配置文件加载配置, 在读代码时可以先跳过这部分, 从 main_function() 开始看
ColorfulPyPrint_set_verbose_level(verbose_level)
my_host_name_no_port = my_host_name  # 不带有端口号的本机域名

if my_host_port is not None:
    my_host_name += ':' + str(my_host_port)  # 带有端口号的本机域名, 如果为标准端口则不带显式端口号
    my_host_name_urlencoded = quote_plus(my_host_name)  # url编码后的
else:
    my_host_name_urlencoded = my_host_name

if external_domains is None:
    external_domains = []
external_domains = list([d.strip("./ \t").replace("https://", "").replace("http://", "") for d in external_domains])

external_domains_set = set(external_domains or [])
allowed_domains_set = external_domains_set.copy()
allowed_domains_set.add(target_domain)
for _domain in external_domains:  # for support domain with port
    allowed_domains_set.add(urlsplit('http://' + _domain).hostname)

domain_alias_to_target_set = set()  # 那些被视为主域名的域名, 如 www.google.com和google.com可以都被视为主域名
domain_alias_to_target_set.add(target_domain)
domains_alias_to_target_domain = list(domains_alias_to_target_domain)
if domains_alias_to_target_domain:
    for _domain in domains_alias_to_target_domain:
        allowed_domains_set.add(_domain)
        domain_alias_to_target_set.add(_domain)
    domains_alias_to_target_domain.append(target_domain)
else:
    domains_alias_to_target_domain = [target_domain]
my_host_scheme_escaped = my_host_scheme.replace('/', r'\/')
myurl_prefix = my_host_scheme + my_host_name  # http(s)://www.my-mirror-site.com  末尾没有反斜线
myurl_prefix_escaped = myurl_prefix.replace('/', r'\/')
cdn_domains_number = len(CDN_domains)
allowed_remote_response_headers = {
    'content-type', 'date', 'expires', 'cache-control', 'last-modified', 'server', 'location',
    'accept-ranges',
    'access-control-allow-origin', 'access-control-allow-headers', 'access-control-allow-methods',
    'access-control-expose-headers', 'access-control-max-age', 'access-control-allow-credentials',
    'timing-allow-origin',
}
allowed_remote_response_headers.update(custom_allowed_remote_headers)
# ## Get Target Domain and MyHostName's Root Domain ##
target_domain_root = extract_root_domain(target_domain)[0]  # type: str
my_host_name_root = extract_root_domain(target_domain)[0]  # type: str

# keep-alive的连接池, 每个域名保持一个keep-alive连接
# 借用requests在同一session中, 自动保持keep-alive的特性
connection_pool_per_domain = {}
if enable_keep_alive_per_domain:
    for _domain in allowed_domains_set:
        connection_pool_per_domain[_domain] = {'session': requests.Session(),}

# ## thread local var ##
# 与flask的request变量功能类似, 存储了一些解析后的请求信息, 在程序中会经常被调用
parse = ZmirrorThreadLocal()

# task_scheduler
task_scheduler = sched.scheduler(time, sleep)
# ########## Handle dependencies #############

# 记录一个URL的一些信息, 以及是否应该使用CDN
# 结构例子见下
url_to_use_cdn = {
    "www.fake-domain.com/folder/foo/bar.png":
        [True,  # Should this url use CDN
         "image/png",  # MIME
         17031,  # size, if size too small, will not redirect to cdn
         ]
}

if not enable_stream_content_transfer:
    steamed_mime_keywords = ()

if not url_custom_redirect_enable:
    url_custom_redirect_list = {}
    url_custom_redirect_regex = ()
    shadow_url_redirect_regex = ()
    plain_replace_domain_alias = []

if isinstance(plain_replace_domain_alias, tuple):
    plain_replace_domain_alias = list(plain_replace_domain_alias)

if not enable_stream_content_transfer:
    enable_stream_transfer_async_preload = False

if not enable_automatic_domains_whitelist:
    domains_whitelist_auto_add_glob_list = tuple()

if not enable_individual_sites_isolation:
    isolated_domains = set()
else:
    for isolated_domain in isolated_domains:
        if isolated_domain not in external_domains_set:
            warnprint('An isolated domain:', isolated_domain,
                      'would not have effect because it did not appears in the `external_domains` list')

if enable_custom_access_cookie_generate_and_verify:
    human_ip_verification_whitelist_from_cookies = False

if not is_use_proxy:
    requests_proxies = None
if human_ip_verification_enabled:
    buff = []
    for network in human_ip_verification_default_whitelist_networks:
        buff.append(ipaddress.ip_network(network, strict=False))
    human_ip_verification_default_whitelist_networks = tuple(buff)
    for question in human_ip_verification_questions:
        human_ip_verification_answers_hash_str += question[1]
else:
    identity_verify_required = False
    human_ip_verification_whitelist_from_cookies = False
    must_verify_cookies = False
if not human_ip_verification_whitelist_from_cookies and not enable_custom_access_cookie_generate_and_verify:
    must_verify_cookies = False

url_rewrite_cache = {}  # an VERY Stupid and VERY Experimental Cache
url_rewrite_cache_hit_count = 0
url_rewrite_cache_miss_count = 0

# ########### PreCompile Regex ###############

# 冒号(colon :)可能的值为:
#    : %3A %253A  完整列表见 tests.TestRegex.REGEX_POSSIBLE_COLON
REGEX_COLON = r"""(?::|%(?:25)?3[Aa])"""
# 斜线(slash /)可能的值为(包括大小写):
# 完整列表见 tests.TestRegex.REGEX_POSSIBLE_COLON
#    / \/ \\/ \\\(N个反斜线)/ %2F %5C%2F %5C%5C(N个5C)%2F %255C%252F %255C%255C%252F \x2F
REGEX_SLASH = r"""(?:\\*(?:/|x2[Ff])|%(?:(?:25)?5[Cc]%)*(?:25)?2[Ff])"""
# 引号 可能值的完整列表见 tests.TestRegex.REGEX_POSSIBLE_QUOTE
# " ' \\(可能有N个反斜线)' \\(可能有N个反斜线)"
# %22 %27 %5C(可能N个5C)%22 %5C(可能N个5C)%27
# %2522 %2527 %255C%2522 %255C%2527
# &quot;
REGEX_QUOTE = r"""(?:\\*["']|%(?:(?:25)?5[Cc]%)*2(?:52)?[27]|&quot;)"""

# 代表本镜像域名的正则
if my_host_port is not None:
    REGEX_MY_HOST_NAME = r'(?:' + re.escape(my_host_name_no_port) + REGEX_COLON + re.escape(str(my_host_port)) \
                         + r'|' + re.escape(my_host_name_no_port) + r')'
else:
    REGEX_MY_HOST_NAME = re.escape(my_host_name)

# Advanced url rewriter, see function response_text_rewrite()
# #### 这个正则表达式是整个程序的最核心的部分, 它的作用是从 html/css/js 中提取出长得类似于url的东西 ####
# 如果需要阅读这个表达式, 请一定要在IDE(如PyCharm)的正则高亮下阅读
# 这个正则并不保证匹配到的东西一定是url, 在 regex_url_reassemble() 中会进行进一步验证是否是url
regex_adv_url_rewriter = re.compile(
    # 前缀, 必须有  'action='(表单) 'href='(链接) 'src=' 'url('(css) '@import'(css) '":'(js/json, "key":"value")
    # \s 表示空白字符,如空格tab
    r"""(?P<prefix>\b(?:(?:src|href|action)\s*=|url\s*\(|@import\s*|"\s*:)\s*)""" +  # prefix, eg: src=
    # 左边引号, 可选 (因为url()允许没有引号). 如果是url以外的, 必须有引号且左右相等(在重写函数中判断, 写在正则里可读性太差)
    r"""(?P<quote_left>["'])?""" +  # quote  "'
    # 域名和协议头, 可选. http:// https:// // http:\/\/ (json) https:\/\/ (json) \/\/ (json)
    r"""(?P<domain_and_scheme>(?P<scheme>(?:https?:)?\\?/\\?/)(?P<domain>(?:[-a-z0-9]+\.)+[a-z]+(?P<port>:\d{1,5})?))?""" +
    # url路径, 含参数 可选
    r"""(?P<path>[^\s;+$?#'"\{}]*?""" +  # full path(with query string)  /foo/bar.js?love=luciaZ
    # 查询字符串, 可选
    r"""(?P<query_string>\?[^\s?#'"]*?)?)""" +  # query string  ?love=luciaZ
    # 右引号(可以是右括弧), 必须
    r"""(?P<quote_right>["')])(?P<right_suffix>\W)""",  # right quote  "'
    flags=re.IGNORECASE
)

# Response Cookies Rewriter, see response_cookie_rewrite()
regex_cookie_rewriter = re.compile(r'\bdomain=(\.?([\w-]+\.)+\w+)\b', flags=re.IGNORECASE)
regex_cookie_path_rewriter = re.compile(r'(?P<prefix>[pP]ath)=(?P<path>[\w\._/-]+?;)')

# Request Domains Rewriter, see client_requests_text_rewrite()
# 该正则用于匹配类似于下面的东西
#   [[[http(s):]//]www.mydomain.com/]extdomains/(https-)target.com
# 兼容各种urlencode/escape
#
# 注意, 若想阅读下面的正则表达式, 请一定要在 Pycharm 的正则高亮下进行
# 否则不对可能的头晕/恶心负责
# 下面那个正则, 在组装以后的样子大概是这样的(已大幅简化):
# 假设b.test.com是本机域名
#   ((https?:/{2})?b\.test\.com/)?extdomains/(https-)?((?:[\w-]+\.)+\w+)\b
#
# 对应的 unittest 见 TestRegex.test__regex_request_rewriter_extdomains()
regex_request_rewriter_extdomains = re.compile(
    r"""(?P<domain_prefix>""" +
    (  # [[[http(s):]//]www.mydomain.com/]
        r"""(?P<scheme>""" +
        (  # [[http(s):]//]
            (  # [http(s):]
                r"""(?:https?(?P<colon>{REGEX_COLON}))?""".format(REGEX_COLON=REGEX_COLON)  # https?:
            ) +
            r"""(?P<scheme_slash>%s)(?P=scheme_slash)""" % REGEX_SLASH  # //
        ) +
        r""")?""" +
        REGEX_MY_HOST_NAME +  # www.mydomain.com[:port] 本部分的正则在上面单独组装
        r"""(?P<slash2>(?(scheme_slash)(?P=scheme_slash)|{REGEX_SLASH}))""".format(REGEX_SLASH=REGEX_SLASH)  # # /
    ) +
    r""")?""" +

    r"""extdomains(?(slash2)(?P=slash2)|{REGEX_SLASH})(?P<is_https>https-)?""".format(
        REGEX_SLASH=REGEX_SLASH) +  # extdomains/(https-)
    r"""(?P<real_domain>(?:[\w-]+\.)+\w+)\b""",  # target.com
    flags=re.IGNORECASE,
)
regex_request_rewriter_main_domain = re.compile(REGEX_MY_HOST_NAME)


# 以下正则为*实验性*的 response_text_basic_rewrite() 的替代品
# 用于函数 response_text_basic_mirrorlization()
# 理论上, 在大量域名的情况下, 会比现有的暴力字符串替换要快, 并且未来可以更强大的域名通配符
# v0.28.0加入, v0.28.3后默认启用
def _regex_generate__basic_mirrorlization():
    """产生 regex_basic_mirrorlization
    用一个函数包裹起来是因为在 try_match_and_add_domain_to_rewrite_white_list()
    中需要动态修改 external_domains, 修改以后可能需要随之生成新的正则, 包裹一下比较容易调用
    """
    from collections import Counter

    # 统计各个后缀出现的频率, 并且按照出现频率降序排列, 有助于提升正则效率
    c = Counter(re.escape(x.split(".")[-1]) for x in allowed_domains_set)
    regex_all_remote_tld = sorted(list(c.keys()), key=lambda x: c[x], reverse=True)

    regex_all_remote_tld = "(?:" + "|".join(regex_all_remote_tld) + ")"
    return re.compile(
        r"""(?:""" +
        (  # [[http(s):]//] or [\?["']] or %27 %22 or &quot;
            r"""(?P<scheme>""" +
            (  # [[http(s):]//]
                (  # [http(s):]
                    r"""(?:https?(?P<colon>{REGEX_COLON}))?""".format(REGEX_COLON=REGEX_COLON)  # https?:
                ) +
                r"""(?P<scheme_slash>%s)(?P=scheme_slash)""" % REGEX_SLASH  # //
            ) +
            r""")""" +
            r"""|""" +
            # [\?["']] or %27 %22 or &quot
            r"""(?P<quote>{REGEX_QUOTE})""".format(REGEX_QUOTE=REGEX_QUOTE)
        ) +
        r""")""" +
        # End prefix.
        # Begin domain
        r"""(?P<domain>([a-zA-Z0-9-]+\.){1,5}%s)\b""" % regex_all_remote_tld +
        # Optional suffix slash
        r"""(?P<suffix_slash>(?(scheme_slash)(?P=scheme_slash)|{SLASH}))?""".format(SLASH=REGEX_SLASH) +

        # right quote (if we have left quote)
        r"""(?(quote)(?P=quote))"""
    )


regex_basic_mirrorlization = _regex_generate__basic_mirrorlization()

# ########## Flask app ###########

app = Flask(  # type: Flask
    __name__ if not unittest_mode
    else 'unittest' + str(random.random()).replace('.', '')
)


# ########## Begin Utils #############
def response_text_basic_mirrorlization(text):
    """
    response_text_basic_rewrite() 的实验性升级版本, 默认启用

    *v0.28.1.dev*
        之前版本是在正则中匹配所有允许的域名, 现在改为匹配所有可能允许的TLD,
        可以带来一些性能的提升, 并且容易进行动态域名添加和通配符支持

    *v0.28.2*
        进一步优化正则, 性能提升 47% 左右 (速度约为传统暴力替换的4.4倍)

    *v0.28.3*
        目前来看该功能工作得相当好, 由实验性特性改为正式使用
        移除旧版 response_text_basic_rewrite(), 只保留一个为了向下兼容的 alias

    :param text: 远程响应文本
    :type text: str
    :return: 重写后的响应文本
    :rtype: str
    """

    def regex_reassemble(m):
        remote_domain = get_group("domain", m)
        if remote_domain not in allowed_domains_set:
            return m.group()

        suffix_slash = get_group("suffix_slash", m)
        slash = get_group("scheme_slash", m) or suffix_slash or "/"

        colon = get_group("colon", m) or guess_colon_from_slash(slash)

        _my_host_name = my_host_name.replace(":", colon) if my_host_port else my_host_name

        if remote_domain not in domain_alias_to_target_set:
            # 外部域名
            core = _my_host_name + slash + "extdomains" + slash + remote_domain + suffix_slash
        else:
            # 主域名
            core = _my_host_name + suffix_slash

        quote = get_group("quote", m)
        if quote:  # "target.domain"
            return quote + core + quote
        else:  # http(s)://target.domain  //target.domain

            if get_group("colon", m):  # http(s)://target.domain
                return my_host_scheme.replace(":", colon).replace("/", slash) + core
            else:  # //target.domain
                return slash * 2 + core

    return regex_basic_mirrorlization.sub(regex_reassemble, text)


def encoding_detect(byte_content):
    """
    试图解析并返回二进制串的编码, 如果失败, 则返回 None
    :param byte_content: 待解码的二进制串
    :type byte_content: bytes
    :return: 编码类型或None
    :rtype: Union[str, None]
    """

    if force_decode_remote_using_encode is not None:
        return force_decode_remote_using_encode
    if possible_charsets:
        for charset in possible_charsets:
            try:
                byte_content.decode(encoding=charset)
            except:
                pass
            else:
                return charset
    if cchardet_available:  # detect the encoding using cchardet (if we have)
        return c_chardet(byte_content)['encoding']

    return None


def cache_clean(is_force_flush=False):
    """
    清理程序运行中产生的垃圾, 在程序运行期间会被自动定期调用
    包括各种重写缓存, 文件缓存等
    默认仅清理过期的
    :param is_force_flush: 是否无视有效期, 清理所有缓存
    :type is_force_flush: bool
    """
    global url_rewrite_cache, cache, url_to_use_cdn, connection_pool_per_domain
    if len(url_rewrite_cache) > 16384:
        url_rewrite_cache.clear()
    if len(url_to_use_cdn) > 40960:
        url_to_use_cdn.clear()

    if enable_keep_alive_per_domain:
        connection_pool_per_domain.clear()

    if local_cache_enable:
        cache.check_all_expire(force_flush_all=is_force_flush)

    if is_force_flush:
        try:
            is_domain_match_glob_whitelist.cache_clear()
            is_content_type_streamed.cache_clear()
            extract_real_url_from_embedded_url.cache_clear()
            embed_real_url_to_embedded_url.cache_clear()
            check_global_ua_pass.cache_clear()
            is_mime_represents_text.cache_clear()
            extract_mime_from_content_type.cache_clear()
            is_content_type_using_cdn.cache_clear()
            is_ua_in_whitelist.cache_clear()
            verify_ip_hash_cookie.cache_clear()
            is_denied_because_of_spider.cache_clear()
            is_ip_not_in_allow_range.cache_clear()
            # client_requests_text_rewrite.cache_clear()
            # extract_url_path_and_query.cache_clear()
        except:
            errprint('ErrorWhenCleaningFunctionLruCache')
            traceback.print_exc()


def cron_task_container(task_dict, add_task_only=False):
    """
    定时任务容器. 调用目标函数, 并在运行结束后创建下一次定时

    :param task_dict: 定时任务的相关参数, dict
      { "target":目标函数(可调用的函数对象,不是函数名字符串) 必须,
        "iterval":任务延时(秒) 可选,
        "priority":优先级 可选,
        "name":定时任务别名 可选
        "args":位置型参数 (arg1,arg2) 可选,
        "kwargs":键值型参数 {key:value,} 可选,
      }
    :param add_task_only: 是否只添加定时任务而不执行
    """
    global task_scheduler
    if not add_task_only:
        # 执行任务
        try:
            infoprint('CronTask:', task_dict.get('name', str(task_dict['target'])), 'Target:', str(task_dict['target']))

            target_func = task_dict.get('target')
            if target_func is None:
                raise ValueError("target is not given in " + str(task_dict))
            target_func(
                *(task_dict.get('args', ())),  # 解开参数以后传递
                **(task_dict.get('kwargs', {}))
            )
        except:
            errprint('ErrorWhenProcessingCronTasks', task_dict)
            traceback.print_exc()

    # 当全局开关关闭时, 自动退出线程
    if not enable_cron_tasks:
        if threading.current_thread() != threading.main_thread():
            exit()
        else:
            return

    # 添加下一次定时任务
    task_scheduler.enter(
        task_dict.get('interval', 300),
        task_dict.get('priority', 999),
        cron_task_container,
        (task_dict,)
    )


def cron_task_host():
    """定时任务宿主, 每分钟检查一次列表, 运行时间到了的定时任务"""
    while True:
        # 当全局开关关闭时, 自动退出线程
        if not enable_cron_tasks:
            if threading.current_thread() != threading.main_thread():
                exit()
            else:
                return

        sleep(60)
        try:
            task_scheduler.run()
        except:
            errprint('ErrorDuringExecutingCronTasks')
            traceback.print_exc()


def add_temporary_domain_alias(source_domain, replaced_to_domain):
    """
    添加临时域名替换列表
    用于纯文本域名替换, 见 `plain_replace_domain_alias` 选项
    :param source_domain: 被替换的域名
    :param replaced_to_domain: 替换成这个域名
    :type source_domain: str
    :type replaced_to_domain: str
    """
    if parse.temporary_domain_alias is None:
        parse.temporary_domain_alias = []
    else:
        parse.temporary_domain_alias = list(parse.temporary_domain_alias)

    parse.temporary_domain_alias.append((source_domain, replaced_to_domain))
    dbgprint('A domain', source_domain, 'to', replaced_to_domain, 'added to temporary_domain_alias',
             parse.temporary_domain_alias)


# noinspection PyGlobalUndefined
def try_match_and_add_domain_to_rewrite_white_list(domain, force_add=False):
    """
    若域名与`domains_whitelist_auto_add_glob_list`中的通配符匹配, 则加入 external_domains 列表
    被加入 external_domains 列表的域名, 会被应用重写机制
    用于在程序运行过程中动态添加域名到external_domains中
    也可在外部函数(custom_func.py)中使用
    关于 external_domains 更详细的说明, 请看 default_config.py 中对应的文档
    :type domain: str
    :type force_add: bool
    :rtype: bool
    """
    global external_domains, external_domains_set, allowed_domains_set, prefix_buff
    global regex_basic_mirrorlization

    if domain is None or not domain:
        return False
    if domain in allowed_domains_set:
        return True
    if not force_add and not is_domain_match_glob_whitelist(domain):
        return False

    infoprint('A domain:', domain, 'was added to external_domains list')

    _buff = list(external_domains)  # external_domains是tuple类型, 添加前需要先转换
    _buff.append(domain)
    external_domains = tuple(_buff)  # 转换回tuple, tuple有一些性能优势
    external_domains_set.add(domain)
    allowed_domains_set.add(domain)

    prefix_buff[domain] = calc_domain_replace_prefix(domain)

    # 重新生成匹配正则
    regex_basic_mirrorlization = _regex_generate__basic_mirrorlization()

    # write log
    try:
        with open(zmirror_root('automatic_domains_whitelist.log'), 'a', encoding='utf-8') as fp:
            fp.write(domain + '\n')
    except:
        traceback.print_exc()

    return True


def decode_mirror_url(mirror_url=None):
    """
    解析镜像url(可能含有extdomains), 并提取出原始url信息
    可以不是完整的url, 只需要有 path 部分即可(query_string也可以有)
    若参数留空, 则使用当前用户正在请求的url
    支持json (处理 \/ 和 \. 的转义)

    :rtype: dict[str, Union[str, bool]]
    :return: {'domain':str, 'is_https':bool, 'path':str, 'path_query':str}
    """
    _is_escaped_dot = False
    _is_escaped_slash = False
    result = {}

    if mirror_url is None:
        input_path_query = extract_url_path_and_query()  # type: str
    else:
        if r'\/' in mirror_url:  # 如果 \/ 在url中, 先反转义, 处理完后再转义回来
            _is_escaped_slash = True
            mirror_url = mirror_url.replace(r'\/', '/')

        if r'\.' in mirror_url:  # 如果 \. 在url中, 先反转义, 处理完后再转义回来
            _is_escaped_dot = True
            mirror_url = mirror_url.replace(r'\.', '.')

        input_path_query = extract_url_path_and_query(mirror_url)  # type: str

    if input_path_query[:12] == '/extdomains/':
        # 12 == len('/extdomains/')
        split = urlsplit("//" + input_path_query[12:].lstrip("/"))  # type: urllib.parse.SplitResult

        real_domain = split.netloc
        real_path_query = (split.path or "/") + (("?" + split.query) if split.query else "")

        if real_domain[:6] == 'https-':
            # 如果显式指定了 /extdomains/https-域名 形式(为了兼容老版本)的, 那么使用https
            real_domain = real_domain[6:]
            _is_https = True
        else:
            # 如果是 /extdomains/域名 形式, 没有 "https-" 那么根据域名判断是否使用HTTPS
            _is_https = is_target_domain_use_https(real_domain)

        real_path_query = client_requests_text_rewrite(real_path_query)

        if _is_escaped_dot: real_path_query = real_path_query.replace('.', r'\.')
        if _is_escaped_slash: real_path_query = s_esc(real_path_query)
        result['domain'] = real_domain
        result['is_https'] = _is_https
        result['path_query'] = real_path_query
        result['path'] = urlsplit(result['path_query']).path
        return result

    input_path_query = client_requests_text_rewrite(input_path_query)

    if _is_escaped_dot: input_path_query = input_path_query.replace('.', r'\.')
    if _is_escaped_slash: input_path_query = s_esc(input_path_query)
    result['domain'] = target_domain
    result['is_https'] = (target_scheme == 'https://')
    result['path_query'] = input_path_query
    result['path'] = urlsplit(result['path_query']).path
    return result


# 函数别名, 为了兼容早期版本的配置文件
extract_from_url_may_have_extdomains = decode_mirror_url


# noinspection PyShadowingNames
def encode_mirror_url(raw_url_or_path, remote_domain=None, is_scheme=None, is_escape=False):
    """convert url from remote to mirror url
    :type raw_url_or_path: str
    :type remote_domain: str
    :type is_scheme: bool
    :type is_escape: bool
    :rtype: str
    """

    if is_escape:
        _raw_url_or_path = raw_url_or_path.replace('r\/', r'/')
    else:
        _raw_url_or_path = raw_url_or_path
    sp = urlsplit(_raw_url_or_path)
    if '/extdomains/' == sp.path[:12]:
        return raw_url_or_path
    domain = remote_domain or sp.netloc or parse.remote_domain or target_domain
    if domain not in allowed_domains_set:
        return raw_url_or_path

    if _raw_url_or_path[:2] == '//':
        our_prefix = '//' + my_host_name
    elif is_scheme or (sp.scheme and is_scheme is not False):
        our_prefix = myurl_prefix
    else:
        our_prefix = ''

    if domain not in domain_alias_to_target_set:
        middle_part = '/extdomains/' + domain
    else:
        middle_part = ''

    result = urljoin(our_prefix + middle_part + '/',
                     extract_url_path_and_query(_raw_url_or_path).lstrip('/'))
    if is_escape:
        result = s_esc(result)

    return result


# 函数别名, 为了兼容早期版本的配置文件
convert_to_mirror_url = encode_mirror_url


def is_target_domain_use_https(target_domain):
    """请求目标域名时是否使用https"""
    if force_https_domains == 'NONE':
        return False
    if force_https_domains == 'ALL':
        return True
    if target_domain in force_https_domains:
        return True
    else:
        return False


def add_ssrf_allowed_domain(domain):
    """添加域名到ssrf白名单, 不支持通配符
    :type domain: str
    """
    global allowed_domains_set
    allowed_domains_set.add(domain)


def generate_error_page(errormsg='Unknown Error', error_code=500, is_traceback=False, content_only=False):
    """

    :type errormsg: Union(str, bytes)
    :type error_code: int
    :type is_traceback: bool
    :rtype: Union[Response, str]
    """
    if is_traceback:
        traceback.print_exc()
        errprint(errormsg)

    if isinstance(errormsg, bytes):
        errormsg = errormsg.decode()

    request_detail = ""
    for attrib in filter(lambda x: x[:2] != '__' and x[-2:] != '__', dir(parse)):
        request_detail += "<tr><td>{attrib}</td><td>{value}</td></tr>" \
            .format(attrib=attrib, value=html_escape(str(parse.__getattribute__(attrib))))

    error_page = """<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>zmirror internal error</title>
<style>code{{background-color: #cccaca;}}</style>
</head>
<body>
<h1>zmirror internal error</h1>
An fatal error occurs. 服务器中运行的zmirror出现一个内部错误.<br>

<hr>
<h2>If you are visitor 如果你是访客</h2>
This site is temporary unavailable because some internal error<br>
Please contact your site admin. <br>
该镜像站暂时出现了临时的内部故障, 请联系网站管理员<br>

<hr>
<h2>If you are admin</h2>
You can find full detail log in your server's log.<br>
For apache, typically at <code>/var/log/apache2/YOUR_SITE_NAME_error.log</code><br>
tips: you can use <code>tail -n 100 -f YOUR_SITE_NAME_error.log</code> to view real-time log<br>
<br>
If you can't solve it by your self, here are some ways may help:<br>
<ul>
    <li>contact the developer by email: <a href="mailto:i@z.codes" target="_blank">aploium &lt;i@z.codes&gt;</a></li>
    <li>seeking for help in zmirror's <a href="https://gitter.im/zmirror/zmirror" target="_blank">online chat room</a></li>
    <li>open an <a href="https://github.com/aploium/zmirror/issues" target="_blank">issue</a> (as an bug report) in github</li>
</ul>
<h3>Detail</h3>
<table border="1"><tr><th>Attrib</th><th>Value</th></tr>
{request_detail}
</table>
<h3>Additional information</h3>
<pre>{errormsg}</pre>
<h3>Traceback</h3>
<pre>{traceback_str}</pre>
<hr>
<div style="font-size: smaller">Powered by <em>zmirror {version}</em><br>
<a href="{official_site}" target="_blank">{official_site}</a></div>
</body></html>""".format(errormsg=errormsg, request_detail=request_detail,
                         traceback_str=html_escape(traceback.format_exc()) if is_traceback else 'None or not displayed',
                         version=CONSTS.__VERSION__, official_site=CONSTS.__GITHUB_URL__
                         )

    if not content_only:
        return make_response(error_page.encode(), error_code)
    else:
        return error_page


def generate_304_response(_content_type=None):
    """:rtype Response"""
    r = Response(content_type=_content_type, status=304)
    r.headers.add('X-Cache', 'FileHit-304')
    return r


def generate_ip_verify_hash(input_dict):
    """
    生成一个标示用户身份的hash
    在 human_ip_verification 功能中使用
    hash一共14位
    hash(前7位+salt) = 后7位 以此来进行验证
    :rtype str
    """
    strbuff = human_ip_verification_answers_hash_str
    for key in input_dict:
        strbuff += key + input_dict[key] + str(random.randint(0, 9000000))
    input_key_hash = hex(zlib.adler32(strbuff.encode(encoding='utf-8')))[2:]
    while len(input_key_hash) < 7:
        input_key_hash += '0'
    output_hash = hex(zlib.adler32((input_key_hash + human_ip_verification_answers_hash_str).encode(encoding='utf-8')))[2:]
    while len(output_hash) < 7:
        output_hash += '0'
    return input_key_hash + output_hash


@lru_cache(maxsize=1024)
def verify_ip_hash_cookie(hash_cookie_value):
    """
    根据cookie中的hash判断是否允许用户访问
    在 human_ip_verification 功能中使用
    hash一共14位
    hash(前7位+salt) = 后7位 以此来进行验证
    :type hash_cookie_value: str
    :rtype: bool
    """
    try:
        input_key_hash = hash_cookie_value[:8]
        output_hash = hash_cookie_value[8:]
        calculated_hash = hex(zlib.adler32(
            (input_key_hash + human_ip_verification_answers_hash_str).encode(encoding='utf-8')
        ))[2:]
        if output_hash == calculated_hash:
            return True
        else:
            return False
    except:
        return False


def update_content_in_local_cache(url, content, method='GET'):
    """更新 local_cache 中缓存的资源, 追加content
    在stream模式中使用"""
    if local_cache_enable and method == 'GET' and cache.is_cached(url):
        info_dict = cache.get_info(url)
        resp = cache.get_obj(url)
        resp.set_data(content)

        # 当存储的资源没有完整的content时, without_content 被设置为true
        # 此时该缓存不会生效, 只有当content被添加后, 缓存才会实际生效
        # 在stream模式中, 因为是先接收http头, 然后再接收内容, 所以会出现只有头而没有内容的情况
        # 此时程序会先将只有头部的响应添加到本地缓存, 在内容实际接收完成后再追加内容
        info_dict['without_content'] = False

        if verbose_level >= 4: dbgprint('LocalCache_UpdateCache', url, content[:30], len(content))
        cache.put_obj(
            url,
            resp,
            obj_size=len(content),
            expires=get_expire_from_mime(parse.mime),
            last_modified=info_dict.get('last_modified'),
            info_dict=info_dict,
        )


def put_response_to_local_cache(url, _our_resp, without_content=False):
    """
    put our response object(headers included) to local cache
    :param without_content: for stream mode use
    :param url: client request url
    :param _our_resp: our response(flask response object) to client, would be storge
    :type url: str
    :type _our_resp: Response
    :type without_content: bool
    """
    # Only cache GET method, and only when remote returns 200(OK) status
    if request.method != 'GET' or _our_resp.status_code != 200:
        return

    dbgprint('PuttingCache:', url, "without_content:", without_content)

    if without_content:
        our_resp = copy.copy(_our_resp)
        our_resp.response = None  # delete iterator
        obj_size = 0
    else:
        our_resp = _our_resp
        obj_size = len(parse.remote_response.content)

    # requests' header are CaseInsensitive
    last_modified = parse.remote_response.headers.get('Last-Modified', None)

    cache.put_obj(
        url,
        our_resp,
        expires=get_expire_from_mime(parse.mime),
        obj_size=obj_size,
        last_modified=last_modified,
        info_dict={'without_content': without_content,
                   'last_modified': last_modified,
                   },
    )


def try_get_cached_response(url, client_header=None):
    """
    尝试从本地缓存中取出响应
    :param url: real url with query string
    :type client_header: dict
    :rtype: Union[Response, None]
    """
    # Only use cache when client use GET
    if local_cache_enable and request.method == 'GET' and cache.is_cached(url):
        if client_header is not None and 'if-modified-since' in client_header and \
                cache.is_unchanged(url, client_header.get('if-modified-since', None)):
            dbgprint('FileCacheHit-304', url)
            return generate_304_response()
        else:
            cached_info = cache.get_info(url)
            if cached_info.get('without_content', False):
                # 关于 without_content 的解释, 请看update_content_in_local_cache()函数
                return None
            # dbgprint('FileCacheHit-200')
            resp = cache.get_obj(url)
            assert isinstance(resp, Response)
            resp.headers.set('x-zmirror-cache', 'FileHit')
            return resp
    else:
        return None


def regex_url_reassemble(match_obj):
    """
    Reassemble url parts split by the regex.
    :param match_obj: match object of stdlib re
    :return: re assembled url string (included prefix(url= etc..) and suffix.)
    :rtype: str
    """

    if match_obj.group() in url_rewrite_cache:  # Read Cache
        global url_rewrite_cache_hit_count
        url_rewrite_cache_hit_count += 1
        return url_rewrite_cache[match_obj.group()]
    else:
        global url_rewrite_cache_miss_count

    prefix = get_group('prefix', match_obj)
    quote_left = get_group('quote_left', match_obj)
    quote_right = get_group('quote_right', match_obj)
    path = get_group('path', match_obj)
    match_domain = get_group('domain', match_obj)
    scheme = get_group('scheme', match_obj)

    whole_match_string = match_obj.group()
    # dbgprint('prefix', prefix, 'quote_left', quote_left, 'quote_right', quote_right,
    #          'path', path, 'match_domain', match_domain, 'scheme', scheme, 'whole', whole_match_string)
    if r"\/" in path or r"\/" in scheme:
        require_slash_escape = True
        path = path.replace(r"\/", "/")
        # domain_and_scheme = domain_and_scheme.replace(r"\/", "/")
    else:
        require_slash_escape = False
    # path must be not blank
    if (not path  # path is blank
        # only url(something) and @import are allowed to be unquoted
        or ('url' not in prefix and 'import' not in prefix) and (not quote_left or quote_right == ')')
        # for "key":"value" type replace, we must have at least one '/' in url path (for the value to be regard as url)
        or (':' in prefix and '/' not in path)
        # if we have quote_left, it must equals to the right
        or (quote_left and quote_left != quote_right)
        # in javascript, those 'path' contains one or only two slash, should not be rewrited (for potential error)
        # or (parse.mime == 'application/javascript' and path.count('/') < 2)
        # in javascript, we only rewrite those with explicit scheme ones.
        # v0.21.10+ in "key":"value" format, we should ignore those path without scheme
        or (not scheme and ('javascript' in parse.mime or '"' in prefix))
        ):
        # dbgprint('returned_un_touch', whole_match_string)
        return whole_match_string

    # v0.19.0+ Automatic Domains Whitelist (Experimental)
    if enable_automatic_domains_whitelist:
        try_match_and_add_domain_to_rewrite_white_list(match_domain)

    # dbgprint('remote_path:', remote_path, 'remote_domain:', remote_domain, 'match_domain', match_domain, v=5)
    # dbgprint(match_obj.groups(), v=5)
    # dbgprint('remote_path:', remote_path, 'remote_domain:', remote_domain, 'match_domain', match_domain, v=5)

    domain = match_domain or parse.remote_domain
    # dbgprint('rewrite match_obj:', match_obj, 'domain:', domain, v=5)
    # skip if the domain are not in our proxy list
    if domain not in allowed_domains_set:
        # dbgprint('return untouched because domain not match', domain, whole_match_string)
        return match_obj.group()  # return raw, do not change

    # this resource's absolute url path to the domain root.
    # dbgprint('match path', path, v=5)
    path = urljoin(parse.remote_path, path)
    # dbgprint('middle path', path, v=5)
    if ':' not in parse.remote_domain:  # the python's builtin urljoin has a bug, cannot join domain with port correctly
        url_no_scheme = urljoin(domain + '/', path.lstrip('/'))
    else:
        url_no_scheme = domain + '/' + path.lstrip('/')

    # dbgprint('url_no_scheme', url_no_scheme)
    # add extdomains prefix in path if need
    if domain in external_domains_set:
        path = '/extdomains/' + url_no_scheme

    # dbgprint('final_path', path, v=5)
    if enable_static_resource_CDN and url_no_scheme in url_to_use_cdn:
        # dbgprint('We Know:', url_no_scheme,v=5)
        _we_knew_this_url = True
        _this_url_mime_cdn = url_to_use_cdn[url_no_scheme][0]
    else:
        # dbgprint('We Don\'t know:', url_no_scheme,v=5)
        _we_knew_this_url = False
        _this_url_mime_cdn = False

    # Apply CDN domain
    if _this_url_mime_cdn:
        # pick an cdn domain due to the length of url path
        # an advantage of choose like this (not randomly), is this can make higher CDN cache hit rate.

        # CDN rewrite, rewrite static resources to cdn domains.
        # A lot of cases included, the followings are just the most typical examples.
        # http(s)://target.com/img/love_lucia.jpg --> http(s)://your.cdn.domains.com/img/love_lucia.jpg
        # http://external.com/css/main.css --> http(s)://your.cdn.domains.com/extdomains/external.com/css/main.css
        # http://external.pw/css/main.css --> http(s)://your.cdn.domains.com/extdomains/external.pw/css/main.css
        replace_to_scheme_domain = my_host_scheme + CDN_domains[zlib.adler32(path.encode()) % cdn_domains_number]

    # else:  # parse.mime == 'application/javascript':
    #     replace_to_scheme_domain = ''  # Do not use explicit url prefix in js, to prevent potential error
    elif not scheme:
        replace_to_scheme_domain = ''
    elif 'http' not in scheme:
        replace_to_scheme_domain = '//' + my_host_name
    else:
        replace_to_scheme_domain = myurl_prefix

    reassembled_url = urljoin(replace_to_scheme_domain, path)
    if _this_url_mime_cdn and cdn_redirect_encode_query_str_into_url:
        reassembled_url = embed_real_url_to_embedded_url(
            reassembled_url,
            url_mime=url_to_use_cdn[url_no_scheme][1],
            escape_slash=require_slash_escape
        )

    if require_slash_escape:
        reassembled_url = s_esc(reassembled_url)

    # reassemble!
    # prefix: src=  quote_left: "
    # path: /extdomains/target.com/foo/bar.js?love=luciaZ
    reassembled = prefix + quote_left + reassembled_url + quote_right + get_group('right_suffix', match_obj)

    # write the adv rewrite cache only if we disable CDN or we known whether this url is CDN-able
    if not enable_static_resource_CDN or _we_knew_this_url:
        url_rewrite_cache[match_obj.group()] = reassembled  # write cache
        url_rewrite_cache_miss_count += 1
    # dbgprint('---------------------', v=5)
    return reassembled


@lru_cache(maxsize=256)
def is_ua_in_whitelist(ua_str):
    """
    当机器人或蜘蛛的请求被ban时, 检查它是否处在允许的白名单内
    被 is_denied_because_of_spider() 调用
    :type ua_str: str
    """
    ua_str = ua_str.lower()
    if global_ua_white_name in ua_str:
        return True
    for allowed_ua in spider_ua_white_list:
        if allowed_ua in ua_str:
            return True
    return False


@lru_cache(maxsize=256)
def is_denied_because_of_spider(ua_str):
    """检查user-agent是否因为是蜘蛛或机器人而需要ban掉"""
    ua_str = ua_str.lower()
    if 'spider' in ua_str or 'bot' in ua_str:
        if is_ua_in_whitelist(ua_str):
            infoprint("A Spider/Bot's access was granted", ua_str)
            return False
        infoprint('A Spider/Bot was denied, UA is:', ua_str)
        return True
    else:
        return False


def load_ip_whitelist_file():
    """从文件加载ip白名单"""
    set_buff = set()
    if os.path.exists(zmirror_root(human_ip_verification_whitelist_file_path)):
        with open(zmirror_root(human_ip_verification_whitelist_file_path), 'r', encoding='utf-8') as fp:
            set_buff.add(fp.readline().strip())
    return set_buff


def append_ip_whitelist_file(ip_to_allow):
    """写入ip白名单到文件"""
    try:
        with open(zmirror_root(human_ip_verification_whitelist_file_path), 'a', encoding='utf-8') as fp:
            fp.write(ip_to_allow + '\n')
    except:
        errprint('Unable to write whitelist file')
        traceback.print_exc()


def ip_whitelist_add(ip_to_allow, info_record_dict=None):
    """添加ip到白名单, 并写入文件"""
    if ip_to_allow in single_ip_allowed_set:
        return
    dbgprint('ip white added', ip_to_allow, 'info:', info_record_dict)
    single_ip_allowed_set.add(ip_to_allow)
    is_ip_not_in_allow_range.cache_clear()
    append_ip_whitelist_file(ip_to_allow)
    # dbgprint(single_ip_allowed_set)
    try:
        with open(zmirror_root(human_ip_verification_whitelist_log), 'a', encoding='utf-8') as fp:
            fp.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " " + ip_to_allow
                     + " " + str(request.user_agent)
                     + " " + repr(info_record_dict) + "\n")
    except:
        errprint('Unable to write log file', os.path.abspath(human_ip_verification_whitelist_log))
        traceback.print_exc()


@lru_cache(maxsize=256)
def is_ip_not_in_allow_range(ip_address):
    """判断ip是否在白名单中"""
    if ip_address in single_ip_allowed_set:
        return False
    ip_address_obj = ipaddress.ip_address(ip_address)
    for allowed_network in human_ip_verification_default_whitelist_networks:
        if ip_address_obj in allowed_network:
            return False
    return True


# ########## End utils ###############


# ################# Begin Server Response Handler #################
def preload_streamed_response_content_async(requests_response_obj, buffer_queue):
    """
    stream模式下, 预读远程响应的content
    :param requests_response_obj:
    :type buffer_queue: queue.Queue
    """
    for particle_content in requests_response_obj.iter_content(stream_transfer_buffer_size):
        try:
            buffer_queue.put(particle_content, timeout=10)
        except queue.Full:
            traceback.print_exc()
            exit()
        if verbose_level >= 3: dbgprint('BufferSize', buffer_queue.qsize())
    buffer_queue.put(None, timeout=10)
    exit()


def iter_streamed_response_async():
    """异步, 一边读取远程响应, 一边发送给用户"""
    total_size = 0
    _start_time = process_time()

    _content_buffer = b''
    _disable_cache_temporary = False

    buffer_queue = queue.Queue(maxsize=stream_transfer_async_preload_max_packages_size)

    t = threading.Thread(
        target=preload_streamed_response_content_async,
        args=(parse.remote_response, buffer_queue),
        daemon=True,
    )
    t.start()

    while True:
        try:
            particle_content = buffer_queue.get(timeout=15)
        except queue.Empty:
            warnprint('WeGotAnSteamTimeout')
            traceback.print_exc()
            return
        buffer_queue.task_done()

        if particle_content is not None:
            # 由于stream的特性, content会被消耗掉, 所以需要额外储存起来
            if local_cache_enable and not _disable_cache_temporary:
                if len(_content_buffer) > 8 * 1024 * 1024:  # 8MB
                    _disable_cache_temporary = True
                    _content_buffer = None
                else:
                    _content_buffer += particle_content

            yield particle_content
        else:
            if local_cache_enable and not _disable_cache_temporary:
                update_content_in_local_cache(parse.remote_url, _content_buffer,
                                              method=parse.remote_response.request.method)
            return

        if verbose_level >= 4:
            total_size += len(particle_content)
            dbgprint('total_size:', total_size, 'total_speed(KB/s):',
                     total_size / 1024 / (process_time() - _start_time + 0.000001))


def copy_response(content=None, is_streamed=False):
    """
    Copy and parse remote server's response headers, generate our flask response object

    :type content: str
    :type is_streamed: bool
    :param content: pre-rewrited response content, bytes
    :return: flask response object
    :rtype: Tuple[Response, float]
    """
    if content is None:
        if is_streamed:
            req_time_body = 0
            content = iter_streamed_response_async()
        else:
            content, req_time_body = response_content_rewrite()
    else:
        req_time_body = 0

    dbgprint('RemoteRespHeaders', parse.remote_response.headers)
    resp = Response(content, status=parse.remote_response.status_code)

    for header_key in parse.remote_response.headers:
        header_key_lower = header_key.lower()
        # Add necessary response headers from the origin site, drop other headers
        if header_key_lower in allowed_remote_response_headers:
            if header_key_lower == 'location':
                _location = parse.remote_response.headers[header_key]
                # try to apply custom rewrite function
                try:
                    if custom_text_rewriter_enable:
                        _loc_rewrite = custom_response_text_rewriter(_location, 'mwm/headers-location', parse.remote_url)
                        if isinstance(_loc_rewrite, str):
                            _location = _loc_rewrite
                except:  # just print err and fallback to normal rewrite
                    return generate_error_page("(LOCATION) Custom Rewrite Function ERROR", is_traceback=True), 0
                resp.headers[header_key] = encode_mirror_url(_location)

            elif header_key_lower == 'content-type':
                # force add utf-8 to content-type if it is text
                if is_mime_represents_text(parse.mime) and 'utf-8' not in parse.content_type:
                    resp.headers[header_key] = parse.mime + '; charset=utf-8'
                else:
                    resp.headers[header_key] = parse.remote_response.headers[header_key]

            elif header_key_lower in ('access-control-allow-origin', 'timing-allow-origin'):
                if custom_allowed_origin is None:
                    resp.headers[header_key] = myurl_prefix
                elif custom_allowed_origin == '_*_':
                    _origin = request.headers.get('origin') or request.headers.get('Origin') or myurl_prefix
                    resp.headers[header_key] = _origin
                else:
                    resp.headers[header_key] = custom_allowed_origin

            else:
                resp.headers[header_key] = parse.remote_response.headers[header_key]

        # If we have the Set-Cookie header, we should extract the raw ones
        #   and then change the cookie domain to our domain
        if header_key_lower == 'set-cookie':
            for cookie_string in response_cookies_deep_copy():
                resp.headers.add('Set-Cookie', response_cookie_rewrite(cookie_string))

    dbgprint('OurRespHeaders:\n', resp.headers)

    return resp, req_time_body


# noinspection PyProtectedMember
def response_cookies_deep_copy():
    """
    It's a BAD hack to get RAW cookies headers, but so far, we don't have better way.
    We'd go DEEP inside the urllib's private method to get raw headers

    raw_headers example:
    [('Cache-Control', 'private'),
    ('Content-Length', '48234'),
    ('Content-Type', 'text/html; Charset=utf-8'),
    ('Server', 'Microsoft-IIS/8.5'),
    ('Set-Cookie','BoardList=BoardID=Show; expires=Mon, 02-May-2016 16:00:00 GMT; path=/'),
    ('Set-Cookie','aspsky=abcefgh; expires=Sun, 24-Apr-2016 16:00:00 GMT; path=/; HttpOnly'),
    ('Set-Cookie', 'ASPSESSIONIDSCSSDSSQ=OGKMLAHDHBFDJCDMGBOAGOMJ; path=/'),
    ('X-Powered-By', 'ASP.NET'),
    ('Date', 'Tue, 26 Apr 2016 12:32:40 GMT')]

    """
    raw_headers = parse.remote_response.raw._original_response.headers._headers
    header_cookies_string_list = []
    for name, value in raw_headers:
        if name.lower() == 'set-cookie':
            if my_host_scheme == 'http://':
                value = value.replace('Secure;', '')
                value = value.replace(';Secure', ';')
                value = value.replace('; Secure', ';')
            if 'httponly' in value.lower():
                if enable_aggressive_cookies_path_rewrite:
                    # 暴力cookie path重写, 把所有path都重写为 /
                    value = regex_cookie_path_rewriter.sub('path=/;', value)
                elif enable_aggressive_cookies_path_rewrite is not None:
                    # 重写HttpOnly Cookies的path到当前url下
                    # eg(/extdomains/a.foobar.com): path=/verify; -> path=/extdomains/a.foobar.com/verify

                    if parse.remote_domain not in domain_alias_to_target_set:  # do not rewrite main domains
                        value = regex_cookie_path_rewriter.sub(
                            '\g<prefix>=/extdomains/' + parse.remote_domain + '\g<path>', value)

            header_cookies_string_list.append(value)
    return header_cookies_string_list


def response_content_rewrite():
    """
    Rewrite requests response's content's url. Auto skip binary (based on MIME).
    :return: List[bytes, float]
    """

    _start_time = process_time()
    _content = parse.remote_response.content
    req_time_body = process_time() - _start_time

    if parse.mime and is_mime_represents_text(parse.mime):
        # Do text rewrite if remote response is text-like (html, css, js, xml, etc..)
        if verbose_level >= 3: dbgprint('Text-like', parse.content_type,
                                        parse.remote_response.text[:15], _content[:15])

        # 自己进行编码检测, 因为 requests 内置的编码检测在天朝GBK面前非常弱鸡
        encoding = encoding_detect(parse.remote_response.content)
        if encoding is not None:
            parse.remote_response.encoding = encoding

        # simply copy the raw text, for custom rewriter function first.
        resp_text = parse.remote_response.text

        if developer_string_trace is not None and developer_string_trace in resp_text:
            infoprint('StringTrace: appears in the RAW remote response text, code line no. ', current_line_number())

        # try to apply custom rewrite function
        try:
            if custom_text_rewriter_enable:
                resp_text2 = custom_response_text_rewriter(resp_text, parse.mime, parse.remote_url)
                if isinstance(resp_text2, str):
                    resp_text = resp_text2
                elif isinstance(resp_text2, tuple) or isinstance(resp_text2, list):
                    resp_text, is_skip_builtin_rewrite = resp_text2
                    if is_skip_builtin_rewrite:
                        infoprint('Skip_builtin_rewrite', request.url)
                        return resp_text.encode(encoding='utf-8')
        except:  # just print err and fallback to normal rewrite
            return generate_error_page(
                'Error in custom response rewrite', is_traceback=True, content_only=True).encode(), req_time_body
        else:
            if developer_string_trace is not None and developer_string_trace in resp_text:
                infoprint('StringTrace: appears after custom text rewrite, code line no. ', current_line_number())

        # then do the normal rewrites
        try:
            resp_text = response_text_rewrite(resp_text)
        except:
            return generate_error_page(
                'Error in builtin response rewrite', is_traceback=True, content_only=True).encode(), req_time_body
        else:
            if developer_string_trace is not None and developer_string_trace in resp_text:
                infoprint('StringTrace: appears after builtin rewrite, code line no. ', current_line_number())

        return resp_text.encode(encoding='utf-8'), req_time_body  # return bytes
    else:
        # simply don't touch binary response content
        dbgprint('Binary', parse.content_type)
        return _content, req_time_body


def response_text_basic_rewrite(*args, **kwargs):
    """本函数在v0.28.3被移除, 对本函数的调用会被映射出去
    如果需要查看本函数代码, 请查看git历史到 v0.28.3 以前
    """
    from warnings import warn
    warn("This function is deprecated since v0.28.3, use response_text_basic_mirrorlization() instead", DeprecationWarning)
    return response_text_basic_mirrorlization(*args, **kwargs)


def response_text_rewrite(resp_text):
    """
    rewrite urls in text-like content (html,css,js)
    :type resp_text: str
    :rtype: str
    """
    # v0.20.6+ plain replace domain alias, support json/urlencoded/json-urlencoded/plain
    if url_custom_redirect_enable:
        for before_replace, after_replace in (plain_replace_domain_alias + parse.temporary_domain_alias):
            dbgprint('plain_replace_domain_alias', before_replace, after_replace, v=4)
            resp_text = resp_text.replace(before_replace, after_replace)

    # v0.9.2+: advanced url rewrite engine
    resp_text = regex_adv_url_rewriter.sub(regex_url_reassemble, resp_text)

    if developer_string_trace is not None and developer_string_trace in resp_text:
        infoprint('StringTrace: appears after advanced rewrite, code line no. ', current_line_number())

    # v0.28.0 实验性功能, 在v0.28.3后默认启用
    resp_text = response_text_basic_mirrorlization(resp_text)

    if developer_string_trace is not None and developer_string_trace in resp_text:
        infoprint('StringTrace: appears after basic mirrorlization, code line no. ', current_line_number())

    # for cookies set string (in js) replace
    # eg: ".twitter.com" --> "foo.com"
    resp_text = resp_text.replace('\".' + target_domain_root + '\"', '\"' + my_host_name_no_port + '\"')
    resp_text = resp_text.replace("\'." + target_domain_root + "\'", "\'" + my_host_name_no_port + "\'")
    resp_text = resp_text.replace("domain=." + target_domain_root, "domain=" + my_host_name_no_port)
    resp_text = resp_text.replace('\"' + target_domain_root + '\"', '\"' + my_host_name_no_port + '\"')
    resp_text = resp_text.replace("\'" + target_domain_root + "\'", "\'" + my_host_name_no_port + "\'")

    if developer_string_trace is not None and developer_string_trace in resp_text:
        infoprint('StringTrace: appears after js cookies string rewrite, code line no. ', current_line_number())

    # resp_text = resp_text.replace('lang="zh-Hans"', '', 1)
    return resp_text


def response_cookie_rewrite(cookie_string):
    """
    rewrite response cookie string's domain to `my_host_name`
    :type cookie_string: str
    """
    cookie_string = regex_cookie_rewriter.sub('domain=' + my_host_name_no_port, cookie_string)
    return cookie_string


# ################# End Server Response Handler #################


# ################# Begin Client Request Handler #################
def assemble_remote_url():
    """
    组装目标服务器URL, 即生成 parse.remote_url 的值
    :rtype: str
    """
    if parse.is_external_domain:
        # 请求的是外部域名 (external domains)
        scheme = 'https://' if parse.is_https else 'http://'
        return urljoin(scheme + parse.remote_domain, parse.remote_path_query)
    else:
        # 请求的是主域名及可以被当做(alias)主域名的域名
        return urljoin(target_scheme + target_domain, parse.remote_path_query)


def ssrf_check_layer_1():
    """
    SSRF防护, 第一层, 在请求刚开始时被调用, 检查域名是否允许
    :return: 如果请求触发了SSRF防护, 则返回True
    :rtype: bool
    """
    # Only external in-zone domains are allowed (SSRF check layer 1)
    if parse.remote_domain not in allowed_domains_set:
        if not try_match_and_add_domain_to_rewrite_white_list(parse.remote_domain):  # 请求的域名是否满足通配符
            if developer_temporary_disable_ssrf_prevention:  # 是否在设置中临时关闭了SSRF防护
                add_ssrf_allowed_domain(parse.remote_domain)
                return False
            else:
                return True
    return False


def extract_client_header():
    """
    Extract necessary client header, filter out some.
    :return: dict client request headers
    """
    outgoing_head = {}
    if verbose_level >= 3: dbgprint('ClientRequestHeaders:', request.headers)
    for head_name, head_value in request.headers:
        head_name_l = head_name.lower()
        if (head_name_l not in ('host', 'content-length', 'content-type')) \
                or (head_name_l == 'content-type' and head_value != ''):
            # For Firefox, they may send 'Accept-Encoding: gzip, deflate, br'
            # For Chrome, they may send 'Accept-Encoding: gzip, deflate, sdch, br'
            #   however, requests cannot decode the br encode, so we have to remove it from the request header.
            if head_name_l == 'accept-encoding' and ('br' in head_value or 'sdch' in head_value):
                _str_buff = ''
                if 'gzip' in head_value:
                    _str_buff += 'gzip, '
                if 'deflate' in head_value:
                    _str_buff += 'deflate'
                if _str_buff:
                    outgoing_head[head_name_l] = _str_buff
            else:
                outgoing_head[head_name_l] = client_requests_text_rewrite(head_value)

    if verbose_level >= 3: dbgprint('FilteredRequestHeaders:', outgoing_head)
    return outgoing_head


# noinspection SpellCheckingInspection
def client_requests_text_rewrite(raw_text):
    """
    Rewrite proxy domain to origin domain, extdomains supported.
    Also Support urlencoded url.
    This usually used in rewriting request params

    eg. http://foo.bar/extdomains/accounts.google.com to http://accounts.google.com
    eg2. foo.bar/foobar to www.google.com/foobar
    eg3. http%3a%2f%2fg.zju.tools%2fextdomains%2Faccounts.google.com%2f233
            to http%3a%2f%2faccounts.google.com%2f233

    :type raw_text: str
    :rtype: str
    """

    def replace_to_real_domain(match_obj):
        scheme = get_group("scheme", match_obj)  # type: str
        colon = match_obj.group("colon")  # type: str
        scheme_slash = get_group("scheme_slash", match_obj)  # type: str
        _is_https = bool(get_group("is_https", match_obj))  # type: bool
        real_domain = match_obj.group("real_domain")  # type: str

        result = ""
        if scheme:
            if "http" in scheme:
                if _is_https or is_target_domain_use_https(real_domain):
                    result += "https" + colon
                else:
                    result += "http" + colon

            result += scheme_slash * 2

        result += real_domain

        return result

    # 使用一个复杂的正则进行替换, 这次替换以后, 理论上所有 extdomains 都会被剔除
    # 详见本文件顶部, regex_request_rewriter_extdomains 本体
    replaced = regex_request_rewriter_extdomains.sub(replace_to_real_domain, raw_text)

    if developer_string_trace is not None and developer_string_trace in replaced:
        infoprint('StringTrace: appears client_requests_text_rewrite, code line no. ', current_line_number())

    # 正则替换掉单独的, 不含 /extdomains/ 的主域名
    replaced = regex_request_rewriter_main_domain.sub(target_domain, replaced)

    # 为了保险起见, 再进行一次裸的替换
    replaced = replaced.replace(my_host_name, target_domain)

    dbgprint('ClientRequestedUrl: ', raw_text, '<- Has Been Rewrited To ->', replaced)
    return replaced


def extract_url_path_and_query(full_url=None, no_query=False):
    """
    Convert http://foo.bar.com/aaa/p.html?x=y to /aaa/p.html?x=y

    :param no_query:
    :type full_url: str
    :param full_url: full url
    :return: str
    """
    if full_url is None:
        full_url = request.url
    split = urlsplit(full_url)
    result = split.path or "/"
    if not no_query and split.query:
        result += '?' + split.query
    return result


# ################# End Client Request Handler #################


# ################# Begin Middle Functions #################
def send_request(url, method='GET', headers=None, param_get=None, data=None):
    """实际发送请求到目标服务器, 对于重定向, 原样返回给用户
    被request_remote_site_and_parse()调用"""
    final_hostname = urlsplit(url).netloc
    dbgprint('FinalRequestUrl', url, 'FinalHostname', final_hostname)
    # Only external in-zone domains are allowed (SSRF check layer 2)
    if final_hostname not in allowed_domains_set and not developer_temporary_disable_ssrf_prevention:
        raise ConnectionAbortedError('Trying to access an OUT-OF-ZONE domain(SSRF Layer 2):', final_hostname)

    # set zero data to None instead of b''
    if not data:
        data = None

    if enable_keep_alive_per_domain:
        if final_hostname not in connection_pool_per_domain:
            connection_pool_per_domain[final_hostname] = {'session': requests.Session()}
        _requester = connection_pool_per_domain[final_hostname]['session']
        _requester.cookies.clear()
    else:
        _requester = requests

    # Send real requests
    req_start_time = process_time()
    r = _requester.request(
        method, url,
        params=param_get, headers=headers, data=data,
        proxies=requests_proxies,
        allow_redirects=False,
        stream=enable_stream_content_transfer,
        verify=not developer_do_not_verify_ssl,
    )
    # remote request time
    req_time = process_time() - req_start_time
    dbgprint('RequestTime:', req_time, v=4)

    # Some debug output
    # print(r.request.headers, r.headers)
    if verbose_level >= 3:
        dbgprint(r.request.method, "FinalSentToRemoteRequestUrl:", r.url, "\nRem Resp Stat: ", r.status_code)
        dbgprint("RemoteRequestHeaders: ", r.request.headers)
        if data:
            dbgprint('RemoteRequestRawData: ', r.request.body)
        dbgprint("RemoteResponseHeaders: ", r.headers)

    return r, req_time


def request_remote_site_and_parse():
    try:  # send request to remote server
        data = request.get_data()  # type: bytes

        # 尝试解析浏览器传入的是否是文本内容
        encoding = encoding_detect(data)
        # 如果是文本内容, 则解码并进行重写, 如果是二进制内容, 则跳过
        if encoding is not None:
            try:
                _data = data.decode(encoding=encoding)  # type: str
            except:
                pass
            else:
                data = client_requests_text_rewrite(_data)  # type: str
                data = data.encode(encoding=encoding)  # type: bytes

        if developer_string_trace is not None and developer_string_trace.encode(encoding="utf-8") in data:
            infoprint('StringTrace: appears after client_requests_bin_rewrite, code line no. ', current_line_number())

        # server's request won't follow 301 or 302 redirection
        parse.remote_response, req_time_headers = send_request(
            parse.remote_url,
            method=request.method,
            headers=parse.client_header,
            data=data,  # client_requests_bin_rewrite(request.get_data()),
        )
        if parse.remote_response.url != parse.remote_url:
            warnprint('requests\'s remote url', parse.remote_response.url
                      , 'does no equals our rewrited url', parse.remote_url)
    except:
        return generate_error_page(errormsg="Error occurs when requesting remote server", is_traceback=True)

    # extract response's mime to thread local var
    parse.content_type = parse.remote_response.headers.get('Content-Type', '') \
                         or parse.remote_response.headers.get('content-type', '')
    parse.mime = extract_mime_from_content_type(parse.content_type)

    # only_serve_static_resources
    if only_serve_static_resources and not is_content_type_using_cdn(parse.content_type):
        return generate_simple_resp_page(b'This site is just for static resources.', error_code=403)

    # is streamed
    is_streamed = enable_stream_content_transfer and is_content_type_streamed(parse.content_type)

    # extract cache control header, if not cache, we should disable local cache
    parse.cache_control = parse.remote_response.headers.get('Cache-Control', '') \
                          or parse.remote_response.headers.get('cache-control', '')
    _response_no_cache = 'no-store' in parse.cache_control or 'must-revalidate' in parse.cache_control

    if verbose_level >= 4:
        dbgprint('Response Content-Type:', parse.content_type,
                 'IsStreamed:', is_streamed,
                 'is_no_cache:', _response_no_cache,
                 'Line', current_line_number(), v=4)

    # add url's MIME info to record, for MIME-based CDN rewrite,
    #   next time we access this url, we would know it's mime
    if enable_static_resource_CDN and not _response_no_cache \
            and parse.remote_response.request.method == 'GET' and parse.remote_response.status_code == 200:
        # we should only cache GET method, and response code is 200
        # noinspection PyUnboundLocalVariable
        if parse.url_no_scheme not in url_to_use_cdn:
            # 记录本URL的信息
            url_to_use_cdn[parse.url_no_scheme] = [False, parse.mime, len(parse.remote_response.content)]

            if is_content_type_using_cdn(parse.mime):
                # mark it to use cdn, and record it's url without scheme.
                # eg: If SERVER's request url is http://example.com/2333?a=x, we record example.com/2333?a=x
                # because the same url for http and https SHOULD be the same, drop the scheme would increase performance
                url_to_use_cdn[parse.url_no_scheme][0] = True  # 标记为使用CDN
                dbgprint('CDN enabled for:', parse.url_no_scheme)
            else:
                dbgprint('CDN disabled for:', parse.url_no_scheme)

    # copy and parse remote response
    resp, req_time_body = copy_response(is_streamed=is_streamed)

    # storge entire our server's response (headers included)
    if local_cache_enable and not _response_no_cache:
        put_response_to_local_cache(parse.remote_url, resp, without_content=is_streamed)

    if parse.start_time is not None and not is_streamed:
        # remote request time should be excluded when calculating total time
        resp.headers.add('X-Header-Req-Time', "%.4f" % req_time_headers)
        resp.headers.add('X-Body-Req-Time', "%.4f" % req_time_body)
        resp.headers.add('X-Compute-Time', "%.4f" % (process_time() - parse.start_time - req_time_headers - req_time_body))

    resp.headers.add('X-Powered-By', 'zmirror/%s' % CONSTS.__VERSION__)

    if developer_dump_all_traffics and not is_streamed:
        if not os.path.exists(zmirror_root('traffic')):
            os.mkdir(zmirror_root('traffic'))
        _time_str = datetime.now().strftime('traffic_%Y-%m-%d_%H-%M-%S')
        try:
            with open(os.path.join(zmirror_root('traffic'), _time_str + '.dump'), 'wb') as fp:
                pickle.dump(
                    (_time_str,
                     (repr(request.url), repr(request.headers), repr(request.get_data())),
                     parse.remote_response, resp
                     ),
                    fp)
        except:
            return generate_error_page(errormsg="Unable to dump traffic", is_traceback=True)

    return resp


def filter_client_request():
    """过滤用户请求, 视情况拒绝用户的访问
    :rtype: Union[Response, None]
    """
    dbgprint('Client Request Url: ', request.url)

    # crossdomain.xml
    if os.path.basename(request.path) == 'crossdomain.xml':
        dbgprint('crossdomain.xml hit from', request.url)
        return crossdomain_xml()

    # Global whitelist ua
    if check_global_ua_pass(str(request.user_agent)):
        return None

    if is_deny_spiders_by_403 and is_denied_because_of_spider(str(request.user_agent)):
        return generate_simple_resp_page(b'Spiders Are Not Allowed To This Site', 403)

    if human_ip_verification_enabled and (
                ((human_ip_verification_whitelist_from_cookies or enable_custom_access_cookie_generate_and_verify)
                 and must_verify_cookies)
            or is_ip_not_in_allow_range(request.remote_addr)
    ):
        dbgprint('ip', request.remote_addr, 'is verifying cookies')
        if 'zmirror_verify' in request.cookies and \
                ((human_ip_verification_whitelist_from_cookies and verify_ip_hash_cookie(request.cookies.get('zmirror_verify')))
                 or (enable_custom_access_cookie_generate_and_verify and custom_verify_access_cookie(
                        request.cookies.get('zmirror_verify'), request))):
            ip_whitelist_add(request.remote_addr, info_record_dict=request.cookies.get('zmirror_verify'))
            dbgprint('add to ip_whitelist because cookies:', request.remote_addr)
        else:
            return redirect(
                "/ip_ban_verify_page?origin=" + base64.urlsafe_b64encode(str(request.url).encode(encoding='utf-8')).decode(
                    encoding='utf-8'),
                code=302)

    return None


def prior_request_redirect():
    """对用户的请求进行按需重定向处理
    与 rewrite_client_request() 不同, 使用301/307等进行外部重定向, 不改变服务器内部数据
    遇到任意一个需要重定向的, 就跳出本函数

    这是第一阶段重定向

    第一阶段重定向, 是在 rewrite_client_request() 内部隐式重写 *之前* 的重定向
    第二阶段重定向, 是在 rewrite_client_request() 内部隐式重写 *之后* 的重定向

    :return: 如果不需要重定向, 则返回None, 否则返回重定向的 Response
    :rtype: Union[Response, None]
    """

    # 非外部域名被错误地当成了外部域名, 则需要重定向修正
    if not parse.is_external_domain and '/extdomains/' == request.path[:12]:
        dbgprint('Requesting main domain in extdomains, redirect back.')
        return redirect(parse.remote_path_query, code=307)

    # 镜像隔离机制, 根据 referer 判断当前所处的镜像, 在子镜像中, 若请求不包含 /extdomains/ 的url, 将会被重定向修正
    if enable_individual_sites_isolation and '/extdomains/' != request.path[:12] and request.headers.get('referer'):
        reference_domain = decode_mirror_url(request.headers.get('referer'))['domain']
        if reference_domain in isolated_domains:
            return redirect(encode_mirror_url(parse.remote_path_query, reference_domain), code=307)

    if url_custom_redirect_enable:
        # 简单的自定义重定向, 详见 config: url_custom_redirect_list
        if request.path in url_custom_redirect_list:
            redirect_to = request.url.replace(request.path, url_custom_redirect_list[request.path], 1)
            dbgprint('Redirect from', request.url, 'to', redirect_to)
            return redirect(redirect_to, code=307)

        # 基于正则的自定义重定向, 详见 config: url_custom_redirect_regex
        for regex_match, regex_replace in url_custom_redirect_regex:
            if re.match(regex_match, parse.remote_path_query, flags=re.IGNORECASE) is not None:
                redirect_to = re.sub(regex_match, regex_replace, parse.remote_path_query, flags=re.IGNORECASE)
                dbgprint('Redirect from', request.url, 'to', redirect_to)
                return redirect(redirect_to, code=307)


def posterior_request_redirect():
    """
    这是第二阶段重定向, 内部隐式重写 *之后* 的重定向
    第一阶段重定向, 是在 rewrite_client_request() 内部隐式重写 *之前* 的重定向
    第二阶段重定向, 是在 rewrite_client_request() 内部隐式重写 *之后* 的重定向

    遇到任意一个需要重定向的, 就跳出本函数

    :return: 如果不需要重定向, 则返回None, 否则返回重定向的 Response
    :rtype: Union[Response, None]
    """

    # CDN软重定向
    # 具体请看 config 中 cdn_redirect_code_if_cannot_hard_rewrite 选项的说明
    if enable_static_resource_CDN:  # CDN总开关
        if (cdn_redirect_code_if_cannot_hard_rewrite  # CDN软(301/307)重定向开关
            # 该URL所对应的资源已知, 即之前已经被成功请求过
            and parse.url_no_scheme in url_to_use_cdn
            # 并且该资源已经被判断为可以应用CDN
            and url_to_use_cdn[parse.url_no_scheme][0]
            # 只缓存 GET 方法的资源
            and request.method == 'GET'
            # 只有超过大小下限才会重定向
            and url_to_use_cdn[parse.url_no_scheme][2] > cdn_soft_redirect_minimum_size
            # 请求者的UA符合CDN提供商的爬虫, 则返回实际的资源
            and not is_ua_in_whitelist(str(request.user_agent))
            ):
            # 下面这个urljoin, 是把形如 https://foo.com/a.png?q=233 的url转化为对应的CDN URL https://cdn.com/a.png?q=233
            redirect_to_url = urljoin(
                my_host_scheme
                # 根据url的crc32取余来选取一个CDN域名
                # 使用crc32, 而不是随机数, 是为了确保相同的URL每次都能应用相同的CDN域名
                # 以增加CDN和缓存命中率
                + CDN_domains[zlib.adler32(parse.url_no_scheme.encode()) % cdn_domains_number],
                extract_url_path_and_query()  # 得到目标url的 /a.png?q=233 这么个部分
            )
            if cdn_redirect_encode_query_str_into_url:
                # 将 ?q=233 这种查询字串编码进path, 详情看config里的说明
                redirect_to_url = embed_real_url_to_embedded_url(
                    redirect_to_url, url_mime=url_to_use_cdn[parse.url_no_scheme][1])

            return redirect(redirect_to_url, code=cdn_redirect_code_if_cannot_hard_rewrite)

    # 本地缓存若命中则直接返回
    if local_cache_enable:
        resp = try_get_cached_response(parse.remote_url, parse.client_header)
        if resp is not None:
            dbgprint('CacheHit,Return')
            if parse.start_time is not None:
                resp.headers.set('X-Compute-Time', "%.4f" % (process_time() - parse.start_time))
            return resp


def rewrite_client_request():
    """
    在这里的所有重写都只作用程序内部, 对请求者不可见
    与 prior_request_redirect() 的外部301/307重定向不同,
    本函数通过改变程序内部变量来起到重定向作用
    返回True表示进行了重定向, 需要重载某些设置, 返回False表示未重定向
    遇到重写后, 不会跳出本函数, 而是会继续下一项. 所以重写顺序很重要
    """
    has_been_rewrited = False

    # ------------- 请求重写代码开始 ----------------
    if cdn_redirect_encode_query_str_into_url:
        real_url = extract_real_url_from_embedded_url(request.url)
        if real_url is not None:
            dbgprint("BeforeEmbeddedExtract:", request.url, " After:", real_url)
            request.url = real_url
            request.path = urlsplit(real_url).path
            has_been_rewrited = True

    if url_custom_redirect_enable and shadow_url_redirect_regex:
        _path_query = extract_url_path_and_query()
        _path_query_raw = _path_query

        for before, after in shadow_url_redirect_regex:
            _path_query = re.sub(before, after, _path_query)
            if _path_query != _path_query_raw:
                dbgprint('ShadowUrlRedirect:', _path_query_raw, 'to', _path_query)
                request.url = myurl_prefix + _path_query
                request.path = urlsplit(_path_query).path
                has_been_rewrited = True
                break
    # ------------- 请求重写代码结束 ----------------

    # 如果进行了重写, 那么 has_been_rewrited 为 True
    # 在 rewrite_client_request() 函数内部会更改 request.url
    # 所以此时需要重新解析一遍
    if has_been_rewrited:
        _temp = decode_mirror_url()
        parse.remote_domain = _temp['domain']  # type: str
        parse.is_https = _temp['is_https']  # type: bool
        parse.remote_path = _temp['path']  # type: str
        parse.remote_path_query = _temp['path_query']  # type: str
        parse.is_external_domain = parse.remote_domain not in domain_alias_to_target_set
        parse.remote_url = assemble_remote_url()  # type: str
        parse.url_no_scheme = parse.remote_url[parse.remote_url.find('//') + 2:]  # type: str

    return has_been_rewrited


# ################# End Middle Functions #################


# ################# Begin Flask #################
@app.route('/zmirror_stat')
def zmirror_status():
    """返回服务器的一些状态信息"""
    if request.remote_addr and request.remote_addr != '127.0.0.1':
        return generate_simple_resp_page(b'Only 127.0.0.1 are allowed', 403)
    output = ""
    output += strx('extract_real_url_from_embedded_url', extract_real_url_from_embedded_url.cache_info())
    output += strx('\nis_content_type_streamed', is_content_type_streamed.cache_info())
    output += strx('\nembed_real_url_to_embedded_url', embed_real_url_to_embedded_url.cache_info())
    output += strx('\ncheck_global_ua_pass', check_global_ua_pass.cache_info())
    output += strx('\nextract_mime_from_content_type', extract_mime_from_content_type.cache_info())
    output += strx('\nis_content_type_using_cdn', is_content_type_using_cdn.cache_info())
    output += strx('\nis_ua_in_whitelist', is_content_type_using_cdn.cache_info())
    output += strx('\nis_mime_represents_text', is_mime_represents_text.cache_info())
    output += strx('\nis_domain_match_glob_whitelist', is_domain_match_glob_whitelist.cache_info())
    output += strx('\nverify_ip_hash_cookie', verify_ip_hash_cookie.cache_info())
    output += strx('\nis_denied_because_of_spider', is_denied_because_of_spider.cache_info())
    output += strx('\nis_ip_not_in_allow_range', is_ip_not_in_allow_range.cache_info())
    output += strx('\n\ncurrent_threads_number', threading.active_count())
    # output += strx('\nclient_requests_text_rewrite', client_requests_text_rewrite.cache_info())
    # output += strx('\nextract_url_path_and_query', extract_url_path_and_query.cache_info())
    output += strx('\n\nurl_rewriter_cache len: ', len(url_rewrite_cache),
                   'Hits:', url_rewrite_cache_hit_count, 'Misses:', url_rewrite_cache_miss_count)

    output += strx('\n----------------\n')
    output += strx('\ndomain_alias_to_target_set', domain_alias_to_target_set)

    return "<pre>" + output + "</pre>\n"


@app.route('/ip_ban_verify_page', methods=['GET', 'POST'])
def ip_ban_verify_page():
    """生成一个身份验证页面"""
    if request.method == 'GET':
        dbgprint('Verifying IP:', request.remote_addr)
        form_body = ''
        for q_id, _question in enumerate(human_ip_verification_questions):
            form_body += r"""%s <input type="text" name="%d" placeholder="%s" style="width: 190px;" /><br/>""" \
                         % (_question[0], q_id, (html_escape(_question[2]) if len(_question) >= 3 else ""))

        for rec_explain_string, rec_name, input_type in human_ip_verification_identity_record:
            form_body += r"""%s %s<input type="%s" name="%s" /><br/>""" % (
                rec_explain_string,
                ('<span style="color: red;">(必填)<span> ' if human_ip_verification_answer_any_one_questions_is_ok else ""),
                html_escape(input_type), html_escape(rec_name))

        if 'origin' in request.args:
            form_body += r"""<input type="hidden" name="origin" value="%s" style="width: 190px;" />""" % html_escape(
                request.args.get('origin'))

        return r"""<!doctype html>
        <html lang="zh-CN">
        <head>
        <meta charset="UTF-8">
        <title>%s</title>
        </head>
        <body>
          <h1>%s</h1>
          <p>这样的验证只会出现一次，通过后您会被加入白名单，之后相同IP的访问不会再需要验证。<br/>
          提示: 由于手机和宽带IP经常会发生改变，您可能会多次看到这一页面。</p>
          %s <br>
          <pre style="border: 1px dashed;">%s</pre>
          <form method='post'>%s<button type='submit'>递交</button>
          </form>
        </body>
        </html>""" % (
            html_escape(human_ip_verification_title), html_escape(human_ip_verification_title),
            ("只需要回答出以下<b>任意一个</b>问题即可" if human_ip_verification_answer_any_one_questions_is_ok
             else "你需要回答出以下<b>所有问题</b>"),
            human_ip_verification_description, form_body)

    elif request.method == 'POST':
        dbgprint('Verifying Request Form', request.form)

        # 遍历所有问题, 看有没有正确回答上来
        for q_id, _question in enumerate(human_ip_verification_questions):
            submitted_answer = request.form.get(str(q_id), '')
            if submitted_answer == '':  # 没有回答这个问题
                if human_ip_verification_answer_any_one_questions_is_ok:  # 如果只需要回答一个, 那么就跳过
                    continue
                else:  # 如果全部都需要回答, 那么报错
                    return generate_simple_resp_page(b'Please answer question: ' + _question[0].encode(), 200)

            if submitted_answer != _question[1]:  # 如果回答了, 但是答案错误
                return generate_simple_resp_page(b'Wrong answer in: ' + _question[0].encode(), 200)
            elif human_ip_verification_answer_any_one_questions_is_ok:
                break  # 只需要正确回答出一个, 就通过

        else:  # 如果在for中是break跳出的, 就不会执行else, 只有正常执行完for才会进入else
            if human_ip_verification_answer_any_one_questions_is_ok:  # 如果只需要回答一个, 进入else表示一个问题都没回答
                return generate_simple_resp_page(b'Please answer at least ONE question', 200)

        record_dict = {}
        for rec_explain_string, rec_name, form_type in human_ip_verification_identity_record:
            if rec_name not in request.form or not request.form[rec_name]:
                return generate_simple_resp_page(b'Param Missing or Blank: ' + rec_explain_string.encode(), 200)
            else:
                record_dict[rec_name] = request.form[rec_name]

        origin = '/'
        if 'origin' in request.form:
            try:
                origin = base64.urlsafe_b64decode(request.form.get('origin')).decode(encoding='utf-8')
            except:
                return generate_error_page(
                    "Unable to decode origin from value:" + html_escape(request.form.get('origin')), is_traceback=True)
            else:
                netloc = urlsplit(origin).netloc
                if netloc and netloc != my_host_name:
                    origin = '/'

        if identity_verify_required:
            if not custom_identity_verify(record_dict):
                return generate_simple_resp_page(b'Verification Failed, please check', 200)

        resp = generate_html_redirect_page(origin, msg=human_ip_verification_success_msg)

        if human_ip_verification_whitelist_from_cookies:
            _hash = generate_ip_verify_hash(record_dict)
            resp.set_cookie(
                'zmirror_verify',
                _hash,
                expires=datetime.now() + timedelta(days=human_ip_verification_whitelist_cookies_expires_days),
                max_age=human_ip_verification_whitelist_cookies_expires_days * 24 * 3600
                # httponly=True,
                # domain=my_host_name
            )
            record_dict['__zmirror_verify'] = _hash

        elif enable_custom_access_cookie_generate_and_verify:
            _hash = custom_generate_access_cookie(record_dict, request)

            dbgprint('SelfGeneratedCookie:', _hash)

            if _hash is None:
                return generate_simple_resp_page(b'Verification Failed, please check', 200)

            resp.set_cookie(
                'zmirror_verify',
                _hash,
                expires=datetime.now() + timedelta(days=human_ip_verification_whitelist_cookies_expires_days),
                max_age=human_ip_verification_whitelist_cookies_expires_days * 24 * 3600
                # httponly=True,
                # domain=my_host_name
            )
            record_dict['__zmirror_verify'] = _hash

        ip_whitelist_add(request.remote_addr, info_record_dict=record_dict)
        return resp


@app.route('/', methods=['GET', 'POST', 'OPTIONS', 'PUT', 'DELETE', 'HEAD', 'PATCH'])
@app.route('/<path:input_path>', methods=['GET', 'POST', 'OPTIONS', 'PUT', 'DELETE', 'HEAD', 'PATCH'])
def zmirror_enter(input_path='/'):
    """入口函数的壳, 只是包了一层异常处理, 实际是 main_function() """
    try:
        resp = main_function(input_path=input_path)
    except:
        return generate_error_page(is_traceback=True)
    else:
        return resp


# noinspection PyUnusedLocal
def main_function(input_path='/'):
    """本程序的实际入口函数
    :rtype: Response
    """
    dbgprint('-----BeginRequest-----')

    # parse 类似于 flask 的 request, 是 zmirror 特有的一个 thread-local 变量
    # 这个变量的重要性不亚于 request, 在 zmirror 各个部分都会用到
    # 其各个变量的含义如下:
    # parse.start_time          处理请求开始的时间, unix 时间戳
    #      .remote_domain       当前请求对应的远程域名
    #      .is_external_domain  远程域名是否是外部域名, 比如google镜像, www.gstatic.com 就是外部域名
    #      .is_https            是否需要用https 来请求远程域名
    #      .remote_url          远程服务器的url, 比如 https://google.com/search?q=233
    #      .url_no_scheme       没有协议前缀的url,比如 google.com/search?q=233 通常在缓存中用
    #      .remote_path_query   对应的远程path+query, 比如 /search?q=2333
    #      .remote_path         对应的远程path,  比如 /search
    #      .client_header       经过转换和重写以后的访问者请求头
    #      .content_type        远程服务器响应头中的 content_type, 比如 "text/plain; encoding=utf-8"
    #      .mime                远程服务器响应的MIME, 比如 "text/html"
    #      .cache_control       远程服务器响应的cache_control内容
    #      .remote_response     远程服务器的响应, requests.Response
    #      .temporary_domain_alias 用于纯文本域名替换, 见 `plain_replace_domain_alias` 选项

    parse.start_time = process_time()  # to display compute time

    # 将用户请求的URL解析为对应的目标服务器URL
    _temp = decode_mirror_url()
    parse.remote_domain = _temp['domain']  # type: str
    parse.is_https = _temp['is_https']  # type: bool
    parse.remote_path = _temp['path']  # type: str
    parse.remote_path_query = _temp['path_query']  # type: str
    parse.is_external_domain = parse.remote_domain not in domain_alias_to_target_set
    parse.remote_url = assemble_remote_url()  # type: str
    parse.url_no_scheme = parse.remote_url[parse.remote_url.find('//') + 2:]  # type: str
    dbgprint('after extract, url:', parse.remote_url, '   path_query:', parse.remote_path_query)

    # 对用户请求进行检查和过滤
    # 不符合条件的请求(比如爬虫)将终止执行
    # 函数不会修改 parse
    r = filter_client_request()
    if r is not None:  # 如果函数返回值不是None, 则表示需要响应给用户
        dbgprint('-----EndRequest(filtered out)-----')
        return r

    # 对用户请求进行第一级重定向(隐式重写前的重定向)
    # 函数不会修改 parse
    r = prior_request_redirect()
    if r is not None:
        return r

    # 进行请求的隐式重写/重定向
    # 隐式重写只对 zmirror 内部生效, 对浏览器透明
    # 重写可能会修改 flask 的内置 request 变量
    # 可能会修改 parse

    has_been_rewrited = rewrite_client_request()

    if ssrf_check_layer_1():
        return generate_simple_resp_page(
            b'SSRF Prevention! Your Domain Are NOT ALLOWED.', 403)

    # 提取出经过必要重写后的客户请求头
    parse.client_header = extract_client_header()  # type: dict

    # 对用户请求进行第二级重定向(隐式重写后的重定向)
    r = posterior_request_redirect()
    if r is not None:
        return r

    resp = request_remote_site_and_parse()

    dbgprint('-----EndRequest-----')
    return resp


@app.route('/crossdomain.xml')
def crossdomain_xml():
    return Response("""<?xml version="1.0"?>
<!DOCTYPE cross-domain-policy SYSTEM "http://www.macromedia.com/xml/dtds/cross-domain-policy.dtd">
<cross-domain-policy>
<allow-access-from domain="*"/>
<site-control permitted-cross-domain-policies="all"/>
<allow-http-request-headers-from domain="*" headers="*" secure="false"/>
</cross-domain-policy>""", content_type='text/x-cross-domain-policy')


@app.route('/about_zmirror')
def about_zmirror():
    return Response("""zmirror
version: {version}
Author: {author}
Github: {github_url}
Note: Love Luciaz Forever!

Mirroring: {source_site}
This site: {my_domain}
""".format(version=CONSTS.__VERSION__, author=CONSTS.__AUTHOR__,
           github_url=CONSTS.__GITHUB_URL__, source_site=target_domain,
           my_domain=my_host_name),
                    content_type='text/plain')


# ################# End Flask #################

# ################# Begin Post (auto)Exec Section #################

# ########### domain replacer prefix string buff ###############
prefix_buff = {}
for _domain in allowed_domains_set:
    prefix_buff[_domain] = calc_domain_replace_prefix(_domain)

if human_ip_verification_enabled:
    single_ip_allowed_set = load_ip_whitelist_file()
else:
    single_ip_allowed_set = set()

try:
    if unittest_mode:
        import importlib

        # 在 unittest 中, 由于 custom_func 也会引用 zmirror
        # 带来一个额外的引用计数
        # 所以在 unittest 中, 每次重载 zmirror 的时候, 都需要重载一次 custom_func
        importlib.reload(importlib.import_module("custom_func"))
    from custom_func import *
except:
    pass

if custom_text_rewriter_enable:
    try:
        from custom_func import custom_response_text_rewriter
    except:
        warnprint('Cannot import custom_response_text_rewriter custom_func.py,'
                  ' `custom_text_rewriter` is now disabled(if it was enabled)')
        raise

if identity_verify_required:
    try:
        from custom_func import custom_identity_verify
    except:
        identity_verify_required = False
        warnprint('Cannot import custom_identity_verify from custom_func.py,'
                  ' `identity_verify` is now disabled (if it was enabled)')
        raise

if enable_custom_access_cookie_generate_and_verify:
    try:
        from custom_func import custom_generate_access_cookie, custom_verify_access_cookie
    except:
        enable_custom_access_cookie_generate_and_verify = False
        errprint('Cannot import custom_generate_access_cookie and custom_generate_access_cookie from custom_func.py,'
                 ' `enable_custom_access_cookie_generate_and_verify` is now disabled (if it was enabled)')
        raise

if enable_cron_tasks:
    for _task_dict in cron_tasks_list:
        try:
            _task_dict['target'] = globals()[_task_dict['target']]
            cron_task_container(_task_dict, add_task_only=True)
        except Exception as e:
            errprint('UnableToInitCronTask', e)
            raise

    th = threading.Thread(target=cron_task_host, daemon=True)
    th.start()

# ################# End Post (auto)Exec Section #################

if __name__ == '__main__':
    errprint('Please use `python3 wsgi.py` to run')
    exit()
