"""Chrome-observed media aliases and destination node defaults."""

from pyv8c.async_api import Browser


async def test_audio_aliases_and_destination_channel_count_mode():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://media.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
          const media = document.createElement('audio');
          const names = ['audio/x-m4a;', 'audio/x-m4a; codecs="mp4a.40.2"',
            'audio/x-m4a; codecs=opus', 'audio/x-mpegurl;',
            'audio/x-mpegurl; codecs=mp3', 'audio/x-mpegurl; codecs=bogus'];
          const live = new AudioContext(), offline = new OfflineAudioContext(1,128,44100);
          const defaults = [live.destination.channelCountMode, offline.destination.channelCountMode];
          live.destination.channelCountMode = 'max';
          const changed = live.destination.channelCountMode; live.close();
          return {media: names.map(name => media.canPlayType(name)), defaults, changed};
        })()''') == {
            'media': ['maybe', 'probably', '', 'maybe', 'probably', ''],
            'defaults': ['explicit', 'explicit'], 'changed': 'max',
        }
    finally:
        (await browser.close())
