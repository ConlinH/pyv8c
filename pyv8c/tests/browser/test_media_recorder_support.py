"""MediaRecorder MIME probes observed in Chrome 155 and the Costco SDK."""

from pyv8c.async_api import Browser


async def _evaluate(script):
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://media.test/', wait_until="commit"))
        return page.evaluate(script)
    finally:
        (await browser.close())


async def test_sdk_recording_mime_queries_return_chrome_booleans():
    result = (await _evaluate('''(() => {
      const inputs = [
        'video/webm', 'audio/webm', 'video/webm;codecs=vp8',
        'video/webm;codecs=daala', 'video/webm;codecs=h264',
        'audio/webm;codecs=opus', 'video/mp4',
        'video/mp4;codecs=avc1.64003E,mp4a.40.2',
        'video/mp4;codecs=avc1.64003E,opus',
        'video/mp4;codecs=avc3.64003E,mp4a.40.2',
        'video/mp4;codecs=avc3.64003E,opus',
        'video/mp4;codecs=hvc1.1.6.L186.B0,mp4a.40.2',
        'video/mp4;codecs=hvc1.1.6.L186.B0,opus',
        'video/mp4;codecs=hev1.1.6.L186.B0,mp4a.40.2',
        'video/mp4;codecs=hev1.1.6.L186.B0,opus',
        'video/mp4;codecs=av01.0.19M.08,mp4a.40.2',
        'video/mp4;codecs=av01.0.19M.08,opus'
      ];
      return inputs.map(type => MediaRecorder.isTypeSupported(type));
    })()'''))
    assert result == [True, True, True, False, True, True, True] + [True] * 10


async def test_recording_support_parses_container_codec_and_errors_like_chrome():
    result = (await _evaluate('''(() => {
      const types = ['', 'video/webm;codecs=vp8,opus',
        'video/webm;codecs=vp8,', 'video/webm;codecs=,vp8',
        'audio/webm;codecs=vp8', 'audio/webm;codecs=opus,pcm',
        'video/mp4;codecs=avc1.foo', 'video/mp4;codecs=avc1.42E01E',
        'video/mp4;codecs=hvc1.bad', 'video/webm;codecs=avc1.foo',
        'video/mp4;codecs=vp09', 'video/mp4;codecs=vp9',
        'VIDEO/WEBM; CODECS="VP8"'];
      let missing;
      try { MediaRecorder.isTypeSupported(); }
      catch (error) { missing = [error.name, error.message]; }
      return {values: types.map(type => MediaRecorder.isTypeSupported(type)), missing};
    })()'''))
    assert result == {
        'values': [True, True, False, False, False, True, False, True,
                   False, True, False, True, True],
        'missing': ['TypeError', "Failed to execute 'isTypeSupported' on 'MediaRecorder':"
                    ' 1 argument required, but only 0 present.'],
    }
