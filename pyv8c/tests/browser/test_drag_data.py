"""The script-created DataTransfer store and its live collection views."""

from pyv8c.async_api import Browser


async def _evaluate(script):
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://drag.test/', wait_until="commit"))
        return page.evaluate(script)
    finally:
        (await browser.close())


async def test_text_items_keep_chrome_order_and_view_identity():
    result = (await _evaluate('''(() => {
      const d=new DataTransfer(), firstItems=d.items, firstFiles=d.files, firstTypes=d.types;
      const before=[d.effectAllowed,d.dropEffect,firstItems===d.items,
                    firstFiles===d.files,firstTypes===d.types,Object.isFrozen(firstTypes)];
      const item=d.items.add('alpha','text/plain');
      d.setData('text/html','<b>x</b>');
      d.setData('TEXT/PLAIN','beta');
      const changed=[firstTypes===d.types,firstTypes.length,Array.from(d.types),
                     d.getData('text/plain'),d.items[1]===item,d.items.length];
      d.items.remove(0); const removed=[Array.from(d.types),d.getData('text/html')];
      d.clearData(); const cleared=[d.items.length,d.files.length,Array.from(d.types)];
      d.effectAllowed='copy'; d.dropEffect='move';
      return {before,changed,removed,cleared,effects:[d.effectAllowed,d.dropEffect]};
    })()'''))
    assert result == {
        'before': ['none', 'none', False, True, True, True],
        'changed': [False, 0, ['text/html', 'text/plain'], 'beta', False, 2],
        'removed': [['text/plain'], ''],
        'cleared': [0, 0, []],
        'effects': ['none', 'none'],
    }


async def test_file_list_is_live_and_clear_data_preserves_files():
    result = (await _evaluate('''(() => {
      const d=new DataTransfer(), files=d.files;
      const file=new File(['abc'],'sample.txt',{type:'text/plain'});
      const item=d.items.add(file);
      const added=[files===d.files,files.length,d.types[0],d.items[0].kind,
                   d.items[0].type,d.items[0].getAsFile().name,item===d.items[0]];
      d.items.add('one','text/plain');
      const mixed=[Array.from(d.types),d.getData('text/plain')];
      d.clearData(); const afterClearData=[d.items.length,files.length,Array.from(d.types)];
      d.items.clear(); const afterClearItems=[d.items.length,files.length,Array.from(d.types)];
      return {added,mixed,afterClearData,afterClearItems};
    })()'''))
    assert result == {
        'added': [True, 1, 'Files', 'file', 'text/plain', 'sample.txt', False],
        'mixed': [['text/plain', 'Files'], 'one'],
        'afterClearData': [1, 1, ['Files']],
        'afterClearItems': [0, 0, []],
    }
