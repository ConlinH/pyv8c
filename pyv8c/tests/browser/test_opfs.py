"""Profile-private OPFS handles hold data, rather than canned browser answers."""
import pytest


def run(page, body):
    page.set_content('<!doctype html><body><iframe></iframe>', url='https://files.test/')
    page.evaluate('window.result = null; (async () => {' + body +
                  '})().catch(e => { result = {unexpected: [e.name, e.message]}; });')
    page.wait_for('result !== null', timeout=3)
    return page.evaluate('result')


def test_directory_root_has_native_identity_and_stable_entry(page):
    result = run(page, '''
      const a=await navigator.storage.getDirectory(), b=await navigator.storage.getDirectory();
      result=[a instanceof FileSystemDirectoryHandle,a instanceof FileSystemHandle,a.kind,a.name,
              Object.prototype.toString.call(a),a===b,await a.isSameEntry(b),await a.resolve(a)];
    ''')
    assert result == [True, True, 'directory', '', '[object FileSystemDirectoryHandle]', False, True, []]


def test_directory_create_resolve_and_remove_preserve_types(page):
    result = run(page, '''
      const root=await navigator.storage.getDirectory();
      const dir=await root.getDirectoryHandle('sub', {create:true});
      const file=await dir.getFileHandle('data.txt', {create:true});
      const errors=[];
      for(const action of [()=>root.getFileHandle('missing'),()=>root.getFileHandle('sub'),
                           ()=>root.removeEntry('sub')]){
        try{await action();}catch(e){errors.push(e.name);}
      }
      const before=[await root.resolve(file),(await dir.resolve(root))===null,file.kind,file.name];
      await root.removeEntry('sub',{recursive:true});
      try{await file.getFile();}catch(e){errors.push(e.name);}
      result=[before,errors];
    ''')
    assert result == [[['sub', 'data.txt'], True, 'file', 'data.txt'],
                      ['NotFoundError', 'TypeMismatchError', 'InvalidModificationError', 'NotFoundError']]


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', 'a\\b'])
def test_invalid_names_reject_without_creating_an_entry(page, name):
    import json
    result = run(page, '''
      const root=await navigator.storage.getDirectory();
      try { await root.getDirectoryHandle(%s,{create:true}); }
      catch(e){result=[e.name,e instanceof TypeError];}
    ''' % json.dumps(name))
    assert result == ['TypeError', True]


def test_file_writes_commit_on_close_and_support_seek_and_truncate(page):
    result = run(page, '''
      const root=await navigator.storage.getDirectory(), file=await root.getFileHandle('a.txt',{create:true});
      const stream=await file.createWritable();
      await stream.write('hello');
      const before=await (await file.getFile()).text();
      await stream.seek(1); await stream.write(new Uint8Array([65,66])); await stream.truncate(4); await stream.close();
      const snapshot=await file.getFile();
      result=[before,await snapshot.text(),snapshot.name,snapshot.size,snapshot instanceof File,
              stream instanceof WritableStream,stream instanceof FileSystemWritableFileStream];
    ''')
    assert result == ['', 'hABl', 'a.txt', 4, True, True, True]


def test_abort_discards_uncommitted_bytes_and_keep_existing_data_works(page):
    result = run(page, '''
      const root=await navigator.storage.getDirectory(), file=await root.getFileHandle('a.txt',{create:true});
      let w=await file.createWritable();await w.write('abc');await w.close();
      w=await file.createWritable({keepExistingData:true});await w.write({type:'write',position:1,data:'Z'});await w.abort('stop');
      const aborted=await (await file.getFile()).text();
      w=await file.createWritable({keepExistingData:true});await w.write({type:'write',position:5,data:'Q'});await w.close();
      result=[aborted,Array.from(new Uint8Array(await (await file.getFile()).arrayBuffer()))];
    ''')
    assert result == ['abc', [97,98,99,0,0,81]]


def test_get_directory_invalid_receiver_uses_rejected_method_realm_promise(page):
    result = run(page, '''
      const child=document.querySelector('iframe').contentWindow;
      const p=child.StorageManager.prototype.getDirectory.call({});
      try{await p;}catch(e){result=[p instanceof Promise,p instanceof child.Promise,
                                  e instanceof TypeError,e instanceof child.TypeError];}
    ''')
    assert result == [False, True, False, True]


def test_worker_opfs_uses_creator_partition_and_worker_realm(page):
    result = run(page, '''
      const root=await navigator.storage.getDirectory();await root.getFileHandle('from-page',{create:true});
      const script=`onmessage=async()=>{try{const p=navigator.storage.getDirectory();const r=await p;
        const f=await r.getFileHandle('from-page');postMessage([p instanceof Promise,r instanceof FileSystemDirectoryHandle,r.kind,f.name]);
        }catch(e){postMessage([e.name,e.message]);}}`;
      const url=URL.createObjectURL(new Blob([script],{type:'text/javascript'}));
      const worker=new Worker(url);result=await new Promise(resolve=>{worker.onmessage=e=>resolve(e.data);worker.postMessage(0);});
      worker.terminate();URL.revokeObjectURL(url);
    ''')
    assert result == [True, True, 'directory', 'from-page']


def test_worker_sync_access_reads_writes_flushes_and_releases_exclusive_lock(page):
    result = run(page, '''
      const script=`onmessage=async()=>{try{
        const r=await navigator.storage.getDirectory(),f=await r.getFileHandle('sync.bin',{create:true});
        const h=await f.createSyncAccessHandle();const wrote=h.write(new Uint8Array([65,66,67]),{at:2});
        let locked;try{await f.createSyncAccessHandle();}catch(e){locked=e.name;}
        const buffer=new Uint8Array(6);const read=h.read(buffer,{at:0});h.flush();const size=h.getSize();h.truncate(4);h.close();h.close();
        const next=await f.createSyncAccessHandle();const final=next.getSize();next.close();
        postMessage([wrote,read,Array.from(buffer),size,final,locked]);
      }catch(e){postMessage([e.name,e.message]);}}`;
      const url=URL.createObjectURL(new Blob([script],{type:'text/javascript'})),w=new Worker(url);
      result=await new Promise(resolve=>{w.onmessage=e=>resolve(e.data);w.postMessage(0);});w.terminate();URL.revokeObjectURL(url);
    ''')
    assert result == [3,5,[0,0,65,66,67,0],5,4,'NoModificationAllowedError']


def test_same_origin_pages_share_data_but_other_origins_and_profiles_do_not(page):
    root_script = '''navigator.storage.getDirectory().then(async root=>{
      try { await root.getFileHandle('shared'); result=true; } catch(e) { result=e.name; }
    });'''
    assert run(page, '''const r=await navigator.storage.getDirectory();
      await r.getFileHandle('shared',{create:true});result=true;''') is True
    profile=page.profile
    other=profile.new_page()
    try:
        other.set_content('<p>same origin</p>',url='https://files.test/other')
        other.evaluate('window.result=null;'+root_script)
        other.wait_for('result!==null',timeout=2)
        assert other.evaluate('result') is True
        foreign=profile.new_page()
        try:
            foreign.set_content('<p>different origin</p>',url='https://elsewhere.test/')
            foreign.evaluate('window.result=null;'+root_script)
            foreign.wait_for('result!==null',timeout=2)
            assert foreign.evaluate('result') == 'NotFoundError'
        finally:foreign.close()
        isolated=profile.browser.new_profile().new_page()
        try:
            isolated.set_content('<p>new profile</p>',url='https://files.test/')
            isolated.evaluate('window.result=null;'+root_script)
            isolated.wait_for('result!==null',timeout=2)
            assert isolated.evaluate('result') == 'NotFoundError'
        finally:isolated.profile.close()
    finally:other.close()
