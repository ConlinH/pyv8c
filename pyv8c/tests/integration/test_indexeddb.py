"""IndexedDB end-to-end battery: storage, cursors, indexes, transaction abort.

``window.indexedDB`` is an in-memory, origin-scoped store inside the realm.
These tests drive it from page scripts and assert on the observable event
model (``upgradeneeded`` / ``success`` / ``complete`` / ``abort``), including
the rollback semantics that matter to real pages:

* an explicit ``abort()`` undoes every request that already succeeded;
* an unhandled request error aborts the transaction and rolls it back;
* ``preventDefault()`` on the error event keeps the transaction alive;
* an aborted ``versionchange`` upgrade keeps the old schema, old version and
  old data, and does not leave a stale connection behind.
"""
from uuid import uuid4

import pytest

pytest.importorskip("pyv8c.pyv8")
pytestmark = pytest.mark.slow
from pyv8c.async_api import Browser

# Narrow bulk-result, generated-key, unique-index and multiEntry behaviour is
# compared with Chrome in tests/repros/corpus.py. This battery retains the
# compound connection, cursor, transaction rollback and schema-upgrade scenarios.

ORIGIN = "http://127.0.0.1:9"


def _loader(url, request):
    return {"status": 200, "url": str(url), "body": "",
            "headers": {"content-type": "text/html"}}


@pytest.fixture
async def page():
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    context = browser.new_profile()
    page = context.new_page()
    await page.goto(ORIGIN + "/")
    try:
        yield page
    finally:
        (await page.close())
        (await browser.close())


async def test_a_stored_arraybuffer_keeps_its_bytes(page):
    """A buffer has nothing enumerable on it, so a clone that walks properties alone
    would store an empty object; what comes back is the buffer a browser stores."""
    page.evaluate("""(() => {
        window.__seen = null;
        const request = indexedDB.open('binary', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('s');
        request.onsuccess = () => {
            const db = request.result;
            const put = db.transaction('s', 'readwrite').objectStore('s')
                          .put(new Uint8Array([1, 2, 3, 4]).buffer, 'k');
            put.onsuccess = () => {
                const get = db.transaction('s').objectStore('s').get('k');
                get.onsuccess = () => {
                    const data = get.result;
                    window.__seen = [data instanceof ArrayBuffer, data.byteLength,
                                     Array.from(new Uint8Array(data)).join(',')];
                };
            };
        };
    })()""")
    await page.wait_for("window.__seen !== null", timeout=10)
    assert page.evaluate("window.__seen") == [True, 4, "1,2,3,4"]


async def run_probe(page, body, timeout=10.0):
    """Evaluate ``body`` (an async script) and return its collected notes."""
    page.evaluate(
        "(() => { globalThis.__idb = null; })()"
    )
    page.evaluate(body)
    await page.wait_for("!!globalThis.__idb && globalThis.__idb.done === true", timeout=timeout)
    result = page.evaluate("globalThis.__idb")
    assert "fatal" not in result, "probe hit a fatal error: %s" % result.get("fatal")
    return result


OPEN_HELPERS = r"""
  function openDB(name, version, upgrade) {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(name, version);
      req.onupgradeneeded = (e) => {
        try { if (upgrade) upgrade(req, e); }
        catch (err) { reject(String(err)); }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error && req.error.name);
    });
  }
  function txDone(db, store, mode, work) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      tx.oncomplete = () => resolve();
      tx.onabort = () => reject(tx.error && tx.error.name);
      work(tx, tx.objectStore(store));
    });
  }
  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
"""


# --------------------------------------------------------------------------
# Smoke: open / upgrade / write / read across connections
# --------------------------------------------------------------------------

async def test_open_crud_and_cross_connection_persistence(page):
    name = "crud_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  try {
    const DB = '__DB__';
    let db = await openDB(DB, 1, (req) => {
      req.result.createObjectStore('books', { keyPath: 'id' });
    });
    out.dbVersion = db.version;
    out.storeNames = Array.from(db.objectStoreNames);

    await txDone(db, 'books', 'readwrite', (tx, s) => {
      s.put({ id: 1, title: 'Alpha' });
      s.put({ id: 2, title: 'Beta' });
      s.put({ id: 3, title: 'Gamma' });
    });

    out.count = await requestResult(db.transaction('books').objectStore('books').count());
    out.titles = (await requestResult(
      db.transaction('books').objectStore('books').getAll()
    )).map((r) => r.title);
    out.missing = (await requestResult(
      db.transaction('books').objectStore('books').get(99)
    )) === undefined ? 'missing' : 'present';

    // a second connection on the same origin sees the committed rows
    const db2 = await openDB(DB, 1);
    const second = await requestResult(db2.transaction('books').objectStore('books').get(2));
    out.reopenedTitle = second && second.title;

    // duplicate add reports ConstraintError but leaves the transaction alive
    out.dupError = await new Promise((resolve) => {
      const tx = db.transaction('books', 'readwrite');
      const req = tx.objectStore('books').add({ id: 1, title: 'dup' });
      req.onerror = (e) => {
        e.preventDefault();
        out.dupErrorName = req.error && req.error.name;
      };
      tx.oncomplete = () => resolve('completed');
      tx.onabort = () => resolve('aborted');
    });
    out.countAfterDup = await requestResult(
      db.transaction('books').objectStore('books').count()
    );
    db2.close(); db.close();
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    assert result["dbVersion"] == 1
    assert result["storeNames"] == ["books"]
    assert result["count"] == 3
    assert result["titles"] == ["Alpha", "Beta", "Gamma"]
    assert result["missing"] == "missing"
    assert result["reopenedTitle"] == "Beta"
    assert result["dupError"] == "completed"
    assert result["dupErrorName"] == "ConstraintError"
    assert result["countAfterDup"] == 3


# --------------------------------------------------------------------------
# Cursors and key ranges
# --------------------------------------------------------------------------

async def test_cursor_directions_ranges_continue_and_advance(page):
    name = "cursor_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  try {
    const DB = '__DB__';
    const db = await openDB(DB, 1, (req) => { req.result.createObjectStore('s'); });
    await txDone(db, 's', 'readwrite', (tx, s) => {
      for (let k = 1; k <= 5; k++) s.put({ v: k * 10 }, k);
    });

    function cursorScan(range, direction) {
      return new Promise((resolve, reject) => {
        const keys = [];
        const req = db.transaction('s').objectStore('s').openCursor(range, direction);
        req.onsuccess = (e) => {
          const cursor = e.target.result;
          if (!cursor) { resolve(keys); return; }
          keys.push(cursor.key);
          cursor.continue();
        };
        req.onerror = () => reject(req.error);
      });
    }

    out.nextKeys = await cursorScan(null, 'next');
    out.prevKeys = await cursorScan(null, 'prev');
    out.lowOpenKeys = await cursorScan(IDBKeyRange.lowerBound(2, true), 'next');
    out.boundKeys = (await requestResult(
      db.transaction('s').objectStore('s').getAll(IDBKeyRange.bound(2, 4))
    )).map((r) => r.v);
    out.boundPrimary = await requestResult(
      db.transaction('s').objectStore('s').getAllKeys(IDBKeyRange.bound(2, 4))
    );

    // continue(key) jumps straight to a later key
    out.jumpKeys = await new Promise((resolve, reject) => {
      const keys = [];
      const req = db.transaction('s').objectStore('s').openCursor();
      req.onsuccess = (e) => {
        const c = e.target.result;
        if (!c) { resolve(keys); return; }
        keys.push(c.key);
        if (keys.length === 1) c.continue(4);
        else c.continue();
      };
      req.onerror = () => reject(req.error);
    });

    // advance() steps by a fixed number of positions
    out.advanceKeys = await new Promise((resolve, reject) => {
      const keys = [];
      const req = db.transaction('s').objectStore('s').openCursor();
      req.onsuccess = (e) => {
        const c = e.target.result;
        if (!c) { resolve(keys); return; }
        keys.push(c.key);
        c.advance(3);
      };
      req.onerror = () => reject(req.error);
    });
    db.close();
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    assert result["nextKeys"] == [1, 2, 3, 4, 5]
    assert result["prevKeys"] == [5, 4, 3, 2, 1]
    assert result["lowOpenKeys"] == [3, 4, 5]
    assert result["boundKeys"] == [20, 30, 40]
    assert result["boundPrimary"] == [2, 3, 4]
    assert result["jumpKeys"] == [1, 4, 5]
    assert result["advanceKeys"] == [1, 4]


# --------------------------------------------------------------------------
# Indexes: get/getAll/count/cursors keyed by index order
# --------------------------------------------------------------------------

async def test_index_lookup_iteration_and_duplicate_index_keys(page):
    name = "index_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  try {
    const DB = '__DB__';
    const db = await openDB(DB, 1, (req) => {
      req.result.createObjectStore('emp', { keyPath: 'id' })
        .createIndex('byName', 'name', { unique: false });
    });
    await txDone(db, 'emp', 'readwrite', (tx, s) => {
      s.put({ id: 1, name: 'Ada', team: 'X' });
      s.put({ id: 2, name: 'Bob', team: 'X' });
      s.put({ id: 3, name: 'Cara', team: 'X' });
      s.put({ id: 4, name: 'Bob', team: 'Y' });   // non-unique index keeps dup
    });

    const store = db.transaction('emp').objectStore('emp');
    const idx = store.index('byName');
    out.indexNames = Array.from(store.indexNames);
    out.getBob = (await requestResult(idx.get('Bob'))).id;
    out.getKeyCara = await requestResult(idx.getKey('Cara'));
    out.allBob = (await requestResult(idx.getAll(IDBKeyRange.only('Bob')))).map((r) => r.id);
    out.bobKeys = await requestResult(idx.getAllKeys(IDBKeyRange.only('Bob')));
    out.indexCount = await requestResult(idx.count());
    out.storeCount = await requestResult(store.count());

    const walk = [];
    await new Promise((resolve, reject) => {
      const req = idx.openCursor();
      req.onsuccess = (e) => {
        const c = e.target.result;
        if (!c) { resolve(); return; }
        walk.push([c.key, c.primaryKey]);
        c.continue();
      };
      req.onerror = () => reject(req.error);
    });
    out.walk = walk;
    db.close();
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    assert result["indexNames"] == ["byName"]
    assert result["getBob"] == 2
    assert result["getKeyCara"] == 3
    assert result["allBob"] == [2, 4]
    assert result["bobKeys"] == ["Bob", "Bob"]
    assert result["indexCount"] == 4
    assert result["storeCount"] == 4
    # index order groups duplicate index keys by primary key
    assert result["walk"] == [["Ada", 1], ["Bob", 2], ["Bob", 4], ["Cara", 3]]


# --------------------------------------------------------------------------
# Transaction abort rollback semantics
# --------------------------------------------------------------------------

async def test_abort_rolls_back_data_and_indexes(page):
    name = "abort_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  try {
    const DB = '__DB__';
    const db = await openDB(DB, 1, (req) => {
      req.result.createObjectStore('people', { keyPath: 'id' })
        .createIndex('byName', 'name', { unique: false });
    });
    await txDone(db, 'people', 'readwrite', (tx, s) => {
      s.put({ id: 1, name: 'Adam', team: 'X' });
      s.put({ id: 2, name: 'Bob', team: 'Y' });
    });
    const snap = async () => {
      const tx = db.transaction('people', 'readonly');
      const store = tx.objectStore('people');
      const ids = (await requestResult(store.getAll())).map((r) => r.id);
      const names = (await requestResult(store.index('byName').getAllKeys())).sort();
      const count = await requestResult(store.index('byName').count());
      return { ids, names, count };
    };

    // 1) explicit abort after several requests already succeeded
    out.explicitOutcome = await new Promise((resolve) => {
      const tx = db.transaction('people', 'readwrite');
      const s = tx.objectStore('people');
      s.put({ id: 3, name: 'Cara' });          // new row
      s.delete(1);                             // delete Adam
      s.put({ id: 1, name: 'Alice' });         // replace Adam -> Alice
      s.get(3).onsuccess = () => { tx.abort(); };  // abort after all writes ran
      tx.onabort = () => resolve('aborted');
      tx.oncomplete = () => resolve('completed');
    });
    out.afterExplicit = await snap();

    // 2) an unhandled ConstraintError aborts the transaction and rolls back
    out.autoOutcome = await new Promise((resolve) => {
      const tx = db.transaction('people', 'readwrite');
      const s = tx.objectStore('people');
      s.put({ id: 4, name: 'Dave' });          // would be added...
      s.add({ id: 1, name: 'Dup' });           // ...then this request fails
      tx.onabort = () => resolve(tx.error && tx.error.name);
      tx.oncomplete = () => resolve('completed');
    });
    out.afterAuto = await snap();

    // 3) preventDefault() keeps the transaction alive; failed add is a no-op
    out.preventedOutcome = await new Promise((resolve) => {
      const tx = db.transaction('people', 'readwrite');
      const s = tx.objectStore('people');
      const req = s.add({ id: 1, name: 'Dup2' });
      req.onerror = (e) => { e.preventDefault(); out.preventedError = req.error && req.error.name; };
      tx.oncomplete = () => resolve('completed');
      tx.onabort = () => resolve('aborted');
    });
    out.afterPrevented = await snap();
    db.close();
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    # explicit abort: every successful write was undone
    assert result["explicitOutcome"] == "aborted"
    assert result["afterExplicit"]["ids"] == [1, 2]
    assert result["afterExplicit"]["names"] == ["Adam", "Bob"]
    assert result["afterExplicit"]["count"] == 2

    # auto abort from the failing request
    assert result["autoOutcome"] == "ConstraintError"
    assert result["afterAuto"]["ids"] == [1, 2]
    assert result["afterAuto"]["count"] == 2

    # prevented error: transaction commits, nothing leaked into the store
    assert result["preventedOutcome"] == "completed"
    assert result["preventedError"] == "ConstraintError"
    assert result["afterPrevented"]["ids"] == [1, 2]
    assert result["afterPrevented"]["names"] == ["Adam", "Bob"]


# --------------------------------------------------------------------------
# Version change: an aborted upgrade must fully restore the old state
# --------------------------------------------------------------------------

async def test_aborted_versionchange_keeps_old_schema_version_and_data(page):
    name = "upgr_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  try {
    const DB = '__DB__';
    let db = await openDB(DB, 1, (req) => { req.result.createObjectStore('s'); });
    await txDone(db, 's', 'readwrite', (tx, s) => {
      s.put({ n: 'a' }, 1);
      s.put({ n: 'b' }, 2);
    });
    db.close();

    // upgrade to v2 then abort from inside onupgradeneeded
    out.abortError = await new Promise((resolve) => {
      const req = indexedDB.open(DB, 2);
      req.onupgradeneeded = (e) => {
        const tx = e.target.transaction;
        tx.objectStore('s').put({ n: 'x' }, 1);     // data write
        req.result.createObjectStore('newstore');   // schema addition
        req.result.deleteObjectStore('s');          // schema removal
        tx.abort();
      };
      req.onsuccess = () => resolve('unexpected-success');
      req.onerror = () => resolve(req.error && req.error.name);
    });

    const db2 = await openDB(DB, 1);
    out.versionAfterAbort = db2.version;
    out.storesAfterAbort = Array.from(db2.objectStoreNames).sort();
    out.key1AfterAbort = (await requestResult(
      db2.transaction('s').objectStore('s').get(1)
    )).n;
    db2.close();

    // A follow-up upgrade must not be blocked by a stale connection from the
    // aborted open() and must still succeed.
    let blocked = false;
    const db3 = await new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 3);
      req.onblocked = () => { blocked = true; };
      req.onupgradeneeded = (e) => {
        e.target.transaction.objectStore('s').createIndex('byN', 'n', { unique: true });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error && req.error.name);
    });
    out.blockedAfterAbort = blocked;
    out.versionAfterRecovery = db3.version;
    out.indexNames = Array.from(db3.transaction('s').objectStore('s').indexNames);
    out.indexCount = await requestResult(
      db3.transaction('s').objectStore('s').index('byN').count()
    );
    db3.close();
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    assert result["abortError"] == "AbortError"
    assert result["versionAfterAbort"] == 1
    assert result["storesAfterAbort"] == ["s"]
    assert result["key1AfterAbort"] == "a"
    # regression: no connection leaked from the failed open()
    assert result["blockedAfterAbort"] is False
    assert result["versionAfterRecovery"] == 3
    assert result["indexNames"] == ["byN"]
    assert result["indexCount"] == 2


# --------------------------------------------------------------------------
# Keys, ranges, cmp and DOMException names
# --------------------------------------------------------------------------

async def test_key_ordering_keyrange_and_exception_names(page):
    name = "errs_" + uuid4().hex[:12]
    body = r"""
(async function () {
  const out = {}; globalThis.__idb = out;
""" + OPEN_HELPERS + r"""
  function errorName(fn) {
    try { fn(); return 'none'; }
    catch (err) { return err && err.name || String(err); }
  }
  try {
    // cmp ordering across key types
    out.cmp = [
      indexedDB.cmp(1, 2),
      indexedDB.cmp('a', 'a'),
      indexedDB.cmp('a', 1),
      indexedDB.cmp([1, 2], [1, 3]),
    ];
    out.cmpBad = errorName(() => indexedDB.cmp(true, 1));

    // IDBKeyRange semantics
    const only = IDBKeyRange.only(5);
    const low = IDBKeyRange.lowerBound(3);
    const lowOpen = IDBKeyRange.lowerBound(3, true);
    const upperOpen = IDBKeyRange.upperBound(3, true);
    const both = IDBKeyRange.bound(1, 5);
    out.range = [
      only.includes(5), only.includes(4),
      low.includes(3), lowOpen.includes(3), upperOpen.includes(3),
      both.includes(1), both.includes(5),
    ];
    out.rangeBad = errorName(() => IDBKeyRange.bound(5, 1));

    // open an existing DB at a lower version -> VersionError
    const exists = await openDB('__DB__', 2, (req) => {
      req.result.createObjectStore('st');
    });
    out.versionDown = await new Promise((resolve) => {
      const req = indexedDB.open('__DB__', 1);
      req.onsuccess = () => resolve('unexpected-success');
      req.onerror = () => resolve(req.error && req.error.name);
    });

    // store handle of a finished transaction -> TransactionInactiveError
    out.txInactive = await new Promise((resolve) => {
      const tx = exists.transaction('st', 'readwrite');
      const handle = tx.objectStore('st');
      handle.get(1).onsuccess = () => { /* ignore */ };
      tx.oncomplete = () => resolve(errorName(() => handle.put({ v: 1 }, 1)));
      tx.onabort = () => resolve('tx-aborted');
    });
    out.abortFinished = errorName(() => {
      const tx = exists.transaction('st', 'readwrite');
      tx.abort();
      tx.abort();   // second abort on a finished transaction throws
    });
    exists.close();
    out.delOwn = await new Promise((resolve) => {
      const req = indexedDB.deleteDatabase('__DB__');
      req.onsuccess = () => resolve('deleted');
      req.onerror = () => resolve(req.error && req.error.name);
    });

    // deleteDatabase removes the database from the metadata listing
    const tmp = await openDB('delme', 1, (req) => { req.result.createObjectStore('x'); });
    tmp.close();
    out.delOutcome = await new Promise((resolve) => {
      const req = indexedDB.deleteDatabase('delme');
      req.onsuccess = () => resolve('deleted');
      req.onerror = () => resolve(req.error && req.error.name);
    });
    out.databases = (await indexedDB.databases()).map((d) => d.name);
    out.done = true;
  } catch (err) { out.fatal = String(err); out.done = true; }
})();
""".replace("__DB__", name)

    result = await run_probe(page, body)
    assert result["cmp"] == [-1, 0, 1, -1]
    assert result["cmpBad"] == "DataError"
    assert result["range"] == [True, False, True, False, False, True, True]
    assert result["rangeBad"] == "DataError"
    assert result["versionDown"] == "VersionError"
    assert result["txInactive"] == "TransactionInactiveError"
    assert result["abortFinished"] == "InvalidStateError"
    assert result["delOwn"] == "deleted"
    assert result["delOutcome"] == "deleted"
    assert name not in result["databases"]
    assert "delme" not in result["databases"]


async def test_databases_do_not_leak_across_contexts():
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    try:
        first = browser.new_profile().new_page()
        await first.goto(ORIGIN + "/")
        first.evaluate("""(() => { globalThis.out = null;
            const req = indexedDB.open('leaky', 1);
            req.onupgradeneeded = () => req.result.createObjectStore('s');
            req.onsuccess = () => { req.result.close(); globalThis.out = true; };
            req.onerror = () => { globalThis.out = req.error && req.error.name; };
        })()""")
        await first.wait_for("globalThis.out === true", timeout=10)

        second = browser.new_profile().new_page()
        await second.goto(ORIGIN + "/")
        second.evaluate("""(() => { globalThis.names = null;
            indexedDB.databases().then((dbs) => { globalThis.names = dbs.map((d) => d.name); });
        })()""")
        await second.wait_for("globalThis.names !== null", timeout=10)
        assert second.evaluate("globalThis.names") == []
    finally:
        (await browser.close())
