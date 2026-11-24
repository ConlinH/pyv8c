"""ISO country code to the one language GeoIP writes.

The choice is fixed: the variety with the most speakers where that is
unambiguous, and an official language ICU accepts for a small territory.
The tag is ``lang-REGION`` (``zh-CN``, not ``zh-Hans-CN``) so it starts
with the primary subtag plus a hyphen. A country with no row is an error.
"""
from __future__ import annotations

# country, BCP 47 tag, primary subtag. The tag starts with "{subtag}-".
_ROWS = """
AD ca-AD ca
AE ar-AE ar
AF fa-AF fa
AG en-AG en
AI en-AI en
AL sq-AL sq
AM hy-AM hy
AO pt-AO pt
AQ en-AQ en
AR es-AR es
AS en-AS en
AT de-AT de
AU en-AU en
AW nl-AW nl
AX sv-AX sv
AZ az-AZ az
BA bs-BA bs
BB en-BB en
BD bn-BD bn
BE nl-BE nl
BF fr-BF fr
BG bg-BG bg
BH ar-BH ar
BI rn-BI rn
BJ fr-BJ fr
BL fr-BL fr
BM en-BM en
BN ms-BN ms
BO es-BO es
BQ nl-BQ nl
BR pt-BR pt
BS en-BS en
BT dz-BT dz
BV nb-BV nb
BW en-BW en
BY ru-BY ru
BZ en-BZ en
CA en-CA en
CC en-CC en
CD fr-CD fr
CF fr-CF fr
CG fr-CG fr
CH de-CH de
CI fr-CI fr
CK en-CK en
CL es-CL es
CM fr-CM fr
CN zh-CN zh
CO es-CO es
CR es-CR es
CU es-CU es
CV pt-CV pt
CW nl-CW nl
CX en-CX en
CY el-CY el
CZ cs-CZ cs
DE de-DE de
DJ so-DJ so
DK da-DK da
DM en-DM en
DO es-DO es
DZ ar-DZ ar
EC es-EC es
EE et-EE et
EG ar-EG ar
EH ar-EH ar
ER ti-ER ti
ES es-ES es
ET am-ET am
FI fi-FI fi
FJ en-FJ en
FK en-FK en
FM en-FM en
FO fo-FO fo
FR fr-FR fr
GA fr-GA fr
GB en-GB en
GD en-GD en
GE ka-GE ka
GF fr-GF fr
GG en-GG en
GH en-GH en
GI en-GI en
GL kl-GL kl
GM en-GM en
GN fr-GN fr
GP fr-GP fr
GQ es-GQ es
GR el-GR el
GS en-GS en
GT es-GT es
GU en-GU en
GW pt-GW pt
GY en-GY en
HK zh-HK zh
HM en-HM en
HN es-HN es
HR hr-HR hr
HT ht-HT ht
HU hu-HU hu
ID id-ID id
IE en-IE en
IL he-IL he
IM en-IM en
IN hi-IN hi
IO en-IO en
IQ ar-IQ ar
IR fa-IR fa
IS is-IS is
IT it-IT it
JE en-JE en
JM en-JM en
JO ar-JO ar
JP ja-JP ja
KE sw-KE sw
KG ky-KG ky
KH km-KH km
KI en-KI en
KM fr-KM fr
KN en-KN en
KP ko-KP ko
KR ko-KR ko
KW ar-KW ar
KY en-KY en
KZ kk-KZ kk
LA lo-LA lo
LB ar-LB ar
LC en-LC en
LI de-LI de
LK si-LK si
LR en-LR en
LS en-LS en
LT lt-LT lt
LU lb-LU lb
LV lv-LV lv
LY ar-LY ar
MA ar-MA ar
MC fr-MC fr
MD ro-MD ro
ME sr-ME sr
MF fr-MF fr
MG mg-MG mg
MH en-MH en
MK mk-MK mk
ML fr-ML fr
MM my-MM my
MN mn-MN mn
MO zh-MO zh
MP en-MP en
MQ fr-MQ fr
MR ar-MR ar
MS en-MS en
MT mt-MT mt
MU en-MU en
MV dv-MV dv
MW en-MW en
MX es-MX es
MY ms-MY ms
MZ pt-MZ pt
NA en-NA en
NC fr-NC fr
NE fr-NE fr
NF en-NF en
NG en-NG en
NI es-NI es
NL nl-NL nl
NO nb-NO nb
NP ne-NP ne
NR en-NR en
NU en-NU en
NZ en-NZ en
OM ar-OM ar
PA es-PA es
PE es-PE es
PF fr-PF fr
PG en-PG en
PH fil-PH fil
PK ur-PK ur
PL pl-PL pl
PM fr-PM fr
PN en-PN en
PR es-PR es
PS ar-PS ar
PT pt-PT pt
PW en-PW en
PY es-PY es
QA ar-QA ar
RE fr-RE fr
RO ro-RO ro
RS sr-RS sr
RU ru-RU ru
RW rw-RW rw
SA ar-SA ar
SB en-SB en
SC en-SC en
SD ar-SD ar
SE sv-SE sv
SG en-SG en
SH en-SH en
SI sl-SI sl
SJ nb-SJ nb
SK sk-SK sk
SL en-SL en
SM it-SM it
SN fr-SN fr
SO so-SO so
SR nl-SR nl
SS en-SS en
ST pt-ST pt
SV es-SV es
SX nl-SX nl
SY ar-SY ar
SZ en-SZ en
TC en-TC en
TD fr-TD fr
TF fr-TF fr
TG fr-TG fr
TH th-TH th
TJ tg-TJ tg
TK en-TK en
TL pt-TL pt
TM tk-TM tk
TN ar-TN ar
TO to-TO to
TR tr-TR tr
TT en-TT en
TV en-TV en
TW zh-TW zh
TZ sw-TZ sw
UA uk-UA uk
UG en-UG en
UM en-UM en
US en-US en
UY es-UY es
UZ uz-UZ uz
VA it-VA it
VC en-VC en
VE es-VE es
VG en-VG en
VI en-VI en
VN vi-VN vi
VU bi-VU bi
WF fr-WF fr
WS sm-WS sm
YE ar-YE ar
YT fr-YT fr
ZA en-ZA en
ZM en-ZM en
ZW en-ZW en
XK sq-XK sq
"""


def _languages(text):
    table = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        country, tag, short = line.split()
        if country in table:
            raise RuntimeError(f"duplicate GeoIP language: {country}")
        if not tag.startswith(short + "-"):
            raise RuntimeError(
                f"GeoIP language {country} tag {tag} does not start with {short}-")
        table[country] = (tag, short)
    return table


_LANGUAGES = _languages(_ROWS)


def languages_for_country(code):
    """``(lang-REGION, lang)`` for an ISO 3166-1 alpha-2 code.

    Matching ignores case. An unknown code raises ``ValueError``.
    """
    key = str(code).strip().upper()
    pair = _LANGUAGES.get(key)
    if pair is None:
        raise ValueError(f"no language for country {code!r}")
    return pair
