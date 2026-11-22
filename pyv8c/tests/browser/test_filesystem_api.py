"""The legacy ``webkitRequestFileSystem`` API and the ``File`` global it backs.

The API is an Optional Feature, so this file also pins the two things that make
it load: the ``File`` constructor a page can touch, and the Window method that
materialises the interface group before handing back the first object.
"""
import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<p>fs</p>", url="https://fs.test/page", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    try:
        yield page
    finally:
        (await browser.close())


async def test_file_constructor_takes_parts_and_name(page):
    assert page.evaluate(
        "(() => { const f = new File(['hi'], 'a.txt', {type: 'Text/Plain', lastModified: 42}); "
        "return [f.name, f.size, f.type, f.lastModified, f instanceof Blob]; })()"
    ) == ["a.txt", 2, "text/plain", 42, True]
    assert page.evaluate("new File(['hi'], 'a.txt').lastModified > 0") is True
    assert page.evaluate(
        "(() => { try { new File(['hi']); return 'no throw'; } catch (e) { return e.name; } })()"
    ) == "TypeError"


async def test_window_method_materialises_the_feature_before_handing_back_an_object(page):
    assert page.evaluate("window.webkitRequestFileSystem.length") == 3
    before = page.binding_session.materialized_interfaces
    assert "DOMFileSystem" not in before

    page.evaluate("""
        window.__fs = null;
        window.webkitRequestFileSystem(0, 4 * 1024 * 1024, fs => {
            __fs = [fs.name, fs.root.fullPath, fs.root.isDirectory,
                    fs.root.toURL(), Object.prototype.toString.call(fs.root)];
        });
    """)
    (await page.run_until_idle(timeout=2))

    assert page.evaluate("window.__fs") == [
        "temporary", "/", True, "filesystem:https://fs.test/page/temporary/", "[object DirectoryEntry]",
    ]
    # The group is committed, so the objects a callback receives are complete.
    materialized = set(page.binding_session.materialized_interfaces)
    assert {"DOMFileSystem", "Entry", "DirectoryEntry", "FileEntry"} <= materialized


async def test_invalid_type_is_rejected_synchronously(page):
    assert page.evaluate(
        "(() => { try { window.webkitRequestFileSystem(9, 1024, () => {}); return 'no throw'; } "
        "catch (e) { return e.name; } })()"
    ) == "TypeError"


async def test_writer_reports_the_write_lifecycle(page):
    page.evaluate("""
        window.__log = [];
        window.__file = null;
        window.webkitRequestFileSystem(0, 4 * 1024 * 1024, fs => {
            fs.root.getFile('note.txt', {create: true}, entry => {
                entry.createWriter(writer => {
                    window.__writer = writer;
                    // An unassigned handler reads ``null``: the attribute is a real
                    // IDL slot, not a plain property that would read ``undefined``.
                    window.__unset = [String(writer.onwriteend), String(writer.onwritestart)];
                    writer.onwritestart = () => __log.push('writestart');
                    writer.onwrite = () => __log.push('write');
                    writer.onprogress = event => __log.push('progress:' + event.loaded + '/' + event.total);
                    writer.onwriteend = () => {
                        __log.push('writeend:' + writer.position);
                        entry.file(file => { window.__file = [file.name, file.size, file.type]; });
                    };
                    writer.write('hello file');
                }, () => __log.push('createWriter failed'));
            }, () => __log.push('getFile failed'));
        }, () => __log.push('request failed'));
    """)
    (await page.run_until_idle(timeout=3))

    assert page.evaluate("window.__log") == ["writestart", "write", "progress:10/10", "writeend:10"]
    assert page.evaluate("window.__file") == ["note.txt", 10, ""]
    assert page.evaluate("window.__unset") == ["null", "null"]
    assert page.evaluate("window.__writer.position") == 10
    assert page.evaluate("window.__writer.length") == 10


async def test_directory_reader_and_remove(page):
    page.evaluate("""
        window.__entries = null;
        window.__removed = false;
        window.webkitRequestFileSystem(0, 4 * 1024 * 1024, fs => {
            fs.root.getDirectory('sub', {create: true}, dir => {
                dir.getFile('inner.txt', {create: true}, () => {
                    dir.createReader().readEntries(entries => {
                        window.__entries = entries.map(entry => entry.name + ':' + entry.isFile);
                        dir.removeRecursively(() => { window.__removed = true; });
                    }, () => window.__entries = 'readEntries failed');
                });
            }, () => window.__entries = 'getDirectory failed');
        }, () => window.__entries = 'request failed');
    """)
    (await page.run_until_idle(timeout=3))

    assert page.evaluate("window.__entries") == ["inner.txt:true"]
    assert page.evaluate("window.__removed") is True


async def test_entry_metadata_and_removal(page):
    page.evaluate("""
        window.__entry = null;
        window.__metadata = null;
        window.__gone = false;
        window.webkitRequestFileSystem(0, 4 * 1024 * 1024, fs => {
            fs.root.getFile('gone.txt', {create: true}, entry => {
                entry.getMetadata(metadata => {
                    window.__metadata = [metadata.size, metadata.modificationTime > 0];
                    entry.remove(() => { window.__gone = true; },
                                 () => window.__gone = 'remove failed');
                }, () => window.__metadata = 'getMetadata failed');
            }, () => window.__metadata = 'getFile failed');
        }, () => window.__metadata = 'request failed');
    """)
    (await page.run_until_idle(timeout=3))

    assert page.evaluate("window.__metadata") == [0, True]
    assert page.evaluate("window.__gone") is True
