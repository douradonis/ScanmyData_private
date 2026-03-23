from playwright.sync_api import sync_playwright
u = 'https://e-invoicing.gr/edocuments/ViewInvoice?v=099360436&ag=02231&c=TDa6Ow0%2FdLbjEy44cEMA%2FFCsJnbo775A5%2Bmum76jVopx04QSsAgpmai93RGo9YT9sTGChZex%2FSISNDTPxNJ2Zehmb75fQsqo6CJRnoc8sig%3D#'
seen = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    def on_req(req):
        ru = req.url
        if (('mydata' in ru.lower()) or ('invoice' in ru.lower()) or ('/api/' in ru.lower())) and ru not in seen:
            seen.append(ru)
    page.on('request', on_req)
    page.goto(u, wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(3500)
    txt = page.inner_text('body')
    print('FINAL_URL', page.url)
    print('REQ_COUNT', len(seen))
    for x in seen[:120]:
        print('REQ', x)
    print('BODY_HAS_MARK', ('M.AR.K' in txt) or ('MARK' in txt))
    print('BODY_PREVIEW', txt[:1200].replace('\n', ' | '))
    links = page.eval_on_selector_all('a', "els => els.map(e => ({t:(e.textContent||''), h:(e.getAttribute('href')||'')}))")
    for it in links[:120]:
        t = (it.get('t') or '').strip()
        h = (it.get('h') or '').strip()
        if t or h:
            print('LINK', t[:100], '=>', h[:220])
    b.close()
