'''
builtin post_fetch event handler
special seed handling -- don't count redirs in depth, and add www.seed if naked seed fails (or reverse)
do this by elaborating the work unit to have an arbitrary callback

parse links and embeds, using an algorithm chosen in conf -- cocrawler.cocrawler

parent subsequently calls add_url on them -- cocrawler.cocrawler
'''

import logging
import cgi
from functools import partial
import json

from . import urls
from . import parse
from . import stats
from . import config

LOGGER = logging.getLogger(__name__)


# aiohttp.ClientReponse lacks this method, so...
def is_redirect(response):
    return response.status in (300, 301, 302, 303, 307)


'''
Study redirs at the host level to see if we're systematically getting
redirs from bare hostname to www or http to https, so we can do that
transformation in advance of the fetch.

Try to discover things that look like unknown url shorteners. Known
url shorteners should be treated as high-priority so that we can
capture the real underlying url before it has time to change, or for
the url shortener to go out of business.
'''


def handle_redirect(f, url, ridealong, priority, json_log, crawler):
    resp_headers = f.response.headers
    location = resp_headers.get('location')
    if location is None:
        LOGGER.info('%d redirect for %s has no Location: header', f.response.status, url.url)
        # XXX this raise causes "ERROR:asyncio:Task exception was never retrieved"
        raise ValueError(url.url + ' sent a redirect with no Location: header')
    next_url = urls.URL(location, urljoin=url)

    kind = urls.special_redirect(url, next_url)
    if kind is not None:
        if 'seed' in ridealong:
            prefix = 'redirect seed'
        else:
            prefix = 'redirect'
        stats.stats_sum(prefix+' '+kind, 1)

    # XXX need to handle 'samesurt' case
    if kind is None:
        pass
    elif kind == 'same':
        LOGGER.info('attempted redirect to myself: %s to %s', url.url, next_url.url)
        if 'Set-Cookie' not in resp_headers:
            LOGGER.info('redirect to myself had no cookies.')
            # XXX try swapping www/not-www? or use a non-crawler UA.
            # looks like some hosts have extra defenses on their redir servers!
        else:
            # XXX we should use a cookie jar with this domain?
            pass
        # fall through; will fail seen-url test in addurl
    else:
        # LOGGER.info('special redirect of type %s for url %s', kind, url.url)
        # XXX push this info onto a last-k for the host
        # to be used pre-fetch to mutate urls we think will redir
        pass

    priority += 1
    json_log['redirect'] = next_url.url

    kwargs = {}
    if 'seed' in ridealong:
        if 'seedredirs' in ridealong:
            ridealong['seedredirs'] += 1
        else:
            ridealong['seedredirs'] = 1
        if ridealong['seedredirs'] > (config.read('Seeds', 'SeedRedirCount') or 0):
            del ridealong['seed']
            del ridealong['seedredirs']
        else:
            kwargs['seed'] = ridealong['seed']
            kwargs['seedredirs'] = ridealong['seedredirs']
            if config.read('Seeds', 'SeedRedirsFree'):
                priority -= 1
            json_log['seedredirs'] = ridealong['seedredirs']

    if crawler.add_url(priority, next_url, **kwargs):
        json_log['found_new_links'] = 1


async def post_200(f, url, priority, json_log, crawler):
    resp_headers = f.response.headers
    content_type = resp_headers.get('content-type', 'None')
    # sometimes content_type comes back multiline. whack it with a wrench.
    content_type = content_type.replace('\r', '\n').partition('\n')[0]
    if content_type:
        content_type, _ = cgi.parse_header(content_type)
    else:
        content_type = 'Unknown'
    LOGGER.debug('url %r came back with content type %r', url.url, content_type)
    json_log['content_type'] = content_type
    stats.stats_sum('content-type=' + content_type, 1)
    if crawler.warcwriter is not None:
        # XXX digest?
        crawler.warcwriter.write_request_response_pair(url.url, f.req_headers, f.response.raw_headers, f.body_bytes)

    if content_type == 'text/html':
        try:
            with stats.record_burn('response.text() decode', url=url):
                body = await f.response.text()  # do not use encoding found in the headers -- policy
                # XXX consider using 'ascii' for speed, if all we want to do is regex in it
        except (UnicodeDecodeError, LookupError):
            # LookupError: .text() guessed an encoding that decode() won't understand (wut?)
            # XXX if encoding was in header, maybe I should use it here?
            # XXX can get additional exceptions here, broken tcp connect etc. see list in fetcher
            body = f.body_bytes.decode(encoding='utf-8', errors='replace')

        # headers is a funky object that's allergic to getting pickled.
        # let's make something more boring
        # XXX get rid of this for the one in warc?
        resp_headers_list = []
        for k, v in resp_headers.items():
            resp_headers_list.append((k.lower(), v))

        if len(body) > crawler.burner_parseinburnersize:
            links, embeds, sha1, facets = await crawler.burner.burn(
                partial(parse.do_burner_work_html, body, f.body_bytes, resp_headers_list, url=url),
                url=url)
        else:
            with stats.coroutine_state('await main thread parser'):
                links, embeds, sha1, facets = parse.do_burner_work_html(
                    body, f.body_bytes, resp_headers_list, url=url)
        json_log['checksum'] = sha1

        if crawler.facetlogfd:
            print(json.dumps({'url': url.url, 'facets': facets}, sort_keys=True), file=crawler.facetlogfd)

        LOGGER.debug('parsing content of url %r returned %d links, %d embeds, %d facets',
                     url.url, len(links), len(embeds), len(facets))
        json_log['found_links'] = len(links) + len(embeds)
        stats.stats_max('max urls found on a page', len(links) + len(embeds))

        new_links = 0
        for u in links:
            if crawler.add_url(priority + 1, u):
                new_links += 1
        for u in embeds:
            if crawler.add_url(priority - 1, u):
                new_links += 1

        if new_links:
            json_log['found_new_links'] = new_links

        # XXX plugin for links and new links - post to Kafka, etc