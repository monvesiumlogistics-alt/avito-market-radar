"""Product identity (ADR-022): заголовок объявления → бренд + модель → канонический ключ товара.

Локально и детерминированно: нормализация → реестр брендов → разбор модели сразу после бренда → уверенность.
Главный принцип — ложное слияние хуже ложного разделения: не уверен — LOW/UNKNOWN, ключа нет.

HIGH    бренд + модель с кодом (буквы/цифры): Pioneer DDJ-FLX4, Sony A7 IV, Aimiko U2 Pro
MEDIUM  бренд + полное имя серии без кода (Steam Deck, Pinarello Gan) или модель только из чисел (Honor 16)
LOW     найден только бренд (Stone Island куртка, Lenovo Legion без номера)
UNKNOWN бренда нет

Ключ есть только у HIGH/MEDIUM. Вариант (память, батарея, цвет, год, размер) в ключ не входит.
"""

import re
import unicodedata
from dataclasses import dataclass, field

PRODUCT_EXTRACTOR_VERSION = 2  # v2: ёмкость 40/60/70ah, 71.4v, «+» = plus, амперы у электровелосипедов
CLUSTERABLE = ("HIGH", "MEDIUM")


@dataclass(frozen=True)
class Brand:
    name: str  # как показывать
    aliases: tuple[str, ...]  # нормализованные написания (можно из нескольких слов)
    # серии: «legion» — только с номером (иначе LOW); «=steam deck» — полное имя модели само по себе
    families: tuple[str, ...] = ()
    implied: dict = field(default_factory=dict)  # alias -> серия, которую он подразумевает (iphone -> Apple iphone)
    synonyms: dict = field(default_factory=dict)  # токен -> замена в модели ("" — выкинуть): honor «mb» = magicbook
    domains: tuple[str, ...] = ()  # только для двусмысленных имён (Trek, Focus, Supreme…): где бренд разрешён


def B(name, aliases, families=(), implied=None, synonyms=None, domains=()):  # noqa: N802 — конструктор реестра
    return Brand(name, tuple(aliases), tuple(families), implied or {}, synonyms or {}, tuple(domains))


BIKE, FASHION = ("bike", "ebike"), ("fashion",)  # домены двусмысленных имён (Trek, Focus, Supreme, LV…)

# Реестр брендов: только то, что встречается в данных CORE/WATCH/QUERY или прямо названо в scope. Расширяется руками.
BRANDS: tuple[Brand, ...] = (
    # DJ / студия / синтезаторы
    B("Pioneer", ("pioneer dj", "pioneer", "пионер"), ("ddj", "xdj", "cdj", "djm", "plx")),
    B("AlphaTheta", ("alphatheta", "alpha theta"), ("xdj", "cdj", "djm", "euphonia")),
    B("Denon", ("denon dj", "denon"), ("prime", "sc", "mc", "x")),
    B("Allen & Heath", ("allen & heath", "allen heath", "allen&heath"), ("xone",)),
    B("Technics", ("technics",), ("sl",)),
    B("Numark", ("numark",), ("mixtrack", "ns", "party mix")),
    B("Native Instruments", ("native instruments",), ("traktor", "maschine", "komplete")),
    B("Rane", ("rane",), ("=one", "seventy", "twelve")),
    B("Korg", ("korg",), ("minilogue", "monologue", "volca", "kronos", "nautilus", "opsix", "wavestate", "pa")),
    B("Roland", ("roland",), ("tr", "tb", "jd", "juno", "fantom", "sp", "td", "fp", "rd", "mc")),
    B("Yamaha", ("yamaha",), ("montage", "modx", "psr", "p", "reface", "dgx", "clp")),
    B("Arturia", ("arturia",), ("minilab", "keylab", "microfreak", "minifreak", "polybrute")),
    B("Novation", ("novation",), ("launchpad", "launchkey", "circuit", "peak", "summit")),
    B("Elektron", ("elektron",), ("digitakt", "digitone", "octatrack", "syntakt")),
    B("Teenage Engineering", ("teenage engineering",), ("op",)),
    B("Akai", ("akai",), ("mpc", "mpk", "apc", "force")),
    # фото / видео / экшн
    B("Sony", ("sony", "сони"), ("zv", "fx", "nex", "wh", "wf", "playstation", "ps"),
      synonyms={"alpha": "", "ilce": ""}),
    B("Canon", ("canon", "кэнон", "кенон"), ("eos", "powershot", "g", "rf", "ef")),
    B("Nikon", ("nikon", "никон"), ("z", "d", "coolpix", "monarch", "prostaff")),
    B("Fujifilm", ("fujifilm", "fuji", "фуджи"), ("x", "xt", "x-t", "gfx", "instax")),
    B("Ricoh", ("ricoh",), ("gr",)),
    B("Leica", ("leica", "лейка"), ("q", "m", "d-lux", "monovid", "trinovid", "noctivid")),
    B("Olympus", ("olympus", "olimpus"), ("om", "pen", "mju", "e")),
    B("Panasonic", ("panasonic", "lumix"), ("gh", "g", "s", "lx", "fz", "tz")),
    B("Sigma", ("sigma",), ("fp", "art", "contemporary"), domains=('photo',)),
    B("Contax", ("contax",), ("t2", "t3", "g1", "g2")),
    B("Pentax", ("pentax",), ("k", "kp", "mz", "espio", "papilio")),
    B("GoPro", ("gopro", "go pro", "гопро"), ("hero", "max", "mission", "=fusion")),
    B("DJI", ("dji",), ("mini", "avata", "air", "mavic", "osmo", "osmo action", "osmo pocket", "osmo mobile",
                       "action", "pocket", "neo", "flip", "ronin", "rs", "fpv", "=osmo nano")),  # fmt: skip
    B("Insta360", ("insta360", "insta 360", "инста 360", "инста360"), ("x", "one", "ace", "ace pro", "go")),
    B("Redmagic", ("redmagic", "red magic"), ()),
    # консоли / симрейсинг
    B("Nintendo", ("nintendo",), ("switch", "=switch oled", "=switch lite")),
    B("Valve", ("valve", "steam deck"), ("=steam deck",), {"steam deck": "=steam deck"}),
    B("Moza", ("moza",), ("r", "cs", "crp", "sr", "fsr")),
    B("Fanatec", ("fanatec",), ("csl", "gt dd", "podium", "clubsport")),
    B("Logitech", ("logitech",), ("g",)),
    B("Thrustmaster", ("thrustmaster",), ("t", "tx", "ts")),
    B("Anbernic", ("anbernic",), ("rg",)),
    B("Retroid", ("retroid",), ("pocket",)),
    B("Ayaneo", ("ayaneo",), ("air", "flip", "kun", "next")),
    # ноутбуки / ПК / смартфоны / планшеты
    B("Apple", ("apple", "iphone", "ipad", "macbook", "airpods"),
      ("=xs", "=xs max", "=xr", "iphone", "ipad", "macbook", "macbook air", "macbook pro", "airpods", "watch", "mac"),
      {"iphone": "iphone", "ipad": "ipad", "macbook": "macbook", "airpods": "airpods"}),  # fmt: skip
    B("Lenovo", ("lenovo", "леново"), ("legion", "=legion go", "legion pro", "loq", "thinkpad", "ideapad", "yoga",
                                       "thinkbook", "tab", "legion y")),  # fmt: skip
    B("Xiaomi", ("xiaomi", "сяоми", "ксиоми"), ("redmi", "redmi note", "poco", "redmibook", "mi", "pad", "book",
                                              "electric scooter", "=mi electric scooter pro 2")),  # fmt: skip
    B("Huawei", ("huawei", "хуавей"), ("matebook", "mate", "pura", "p", "nova", "watch", "watch gt", "matepad")),
    B("Honor", ("honor", "хонор", "magicbook"), ("magicbook", "magicbook pro", "magicbook art", "magicbook x", "magic",
                                                 "x",
                                   "pad", "watch", "notebook"), {"magicbook": "magicbook"},
      synonyms={"mb": "magicbook"}),  # fmt: skip
    B("Thunderobot", ("thunderobot",), ("911", "zero", "t-book", "igame")),
    B("MSI", ("msi",), ("katana", "titan", "raider", "stealth", "vector", "cyborg", "thin", "pulse", "sword",
                       "crosshair",
                       "alpha", "bravo", "creator", "modern", "prestige", "claw")),  # fmt: skip
    B("ASUS", ("asus", "асус"), ("rog", "rog strix", "rog zephyrus", "rog flow", "tuf", "zenbook", "vivobook",
                                 "=rog ally", "=rog ally x", "proart")),  # fmt: skip
    B("Alienware", ("alienware", "alineware", "dell alienware"), ("m", "x", "area", "aurora")),
    B("Acer", ("acer",), ("nitro", "predator", "helios", "aspire", "swift")),
    B("HP", ("hp",), ("omen", "victus", "pavilion", "envy", "spectre", "elitebook", "probook")),
    B("Machenike", ("machenike",), ()),
    B("Samsung", ("samsung", "самсунг"), ("galaxy", "galaxy s", "galaxy a", "galaxy z", "galaxy tab", "galaxy watch",
                                         "galaxy buds")),  # fmt: skip
    B("Google", ("google",), ("pixel",)),
    B("OnePlus", ("oneplus", "one plus"), ()),
    B("Realme", ("realme",), ("gt", "c", "note")),
    B("Tecno", ("tecno",), ("spark", "camon", "pova", "phantom")),
    B("Infinix", ("infinix",), ("note", "hot", "zero", "gt")),
    B("NVIDIA", ("nvidia", "geforce"), ("rtx", "gtx")),
    B("AMD", ("amd",), ("rx", "radeon", "ryzen")),
    # электровелосипеды / самокаты
    B("Aimiko", ("aimiko", "аймико"), ()),
    B("Kugoo", ("kugoo", "куго", "кугу"), ("kirin", "wish", "s", "m", "g", "c1", "v1")),
    B("Hualu", ("hualu", "хуалу"), ()),
    B("Wenbox", ("wenbox",), ()),
    B("Fiido", ("fiido",), ()),
    B("ADO", ("ado",), ("a", "air"), domains=('ebike', 'bike')),
    B("Engwe", ("engwe",), ()),
    B("Ninebot", ("ninebot", "segway", "segway ninebot"), ("max", "kickscooter", "f", "e", "p")),
    B("Minako", ("minako",), ()),
    B("Eltreco", ("eltreco",), ()),
    # велосипеды и компоненты
    B("Specialized", ("specialized", "speciliazed", "specialised"), ("tarmac", "roubaix", "allez", "aethos", "venge",
      "diverge", "crux", "epic", "stumpjumper", "chisel", "rockhopper", "s-works"), domains=BIKE),  # fmt: skip
    B("Trek", ("trek",), ("madone", "emonda", "domane", "checkpoint", "fuel", "marlin", "procaliber", "supercaliber"),
      domains=BIKE),
    B("Giant", ("giant",), ("tcr", "defy", "propel", "revolt", "talon", "xtc", "trance", "anthem"), domains=BIKE),
    B("Cannondale", ("cannondale",), ("supersix", "caad", "synapse", "topstone", "scalpel", "trail", "systemsix")),
    B("Canyon", ("canyon",), ("aeroad", "ultimate", "endurace", "grail", "grizl", "lux", "spectral", "neuron"),
      domains=BIKE),
    B("Cube", ("cube",), ("attain", "agree", "litening", "nuroad", "reaction", "stereo", "aim", "ams"), domains=BIKE),
    B("Merida", ("merida",), ("scultura", "reacto", "big nine", "big seven", "ninety-six", "one-twenty"), domains=BIKE),
    B("Pinarello", ("pinarello",), ("dogma", "prince", "paris", "=gan", "=gan disc", "f")),
    B("Bianchi", ("bianchi",), ("oltre", "specialissima", "=infinito", "=infinito cv", "sprint", "via nirone")),
    B("BMC", ("bmc",), ("teammachine", "roadmachine", "timemachine", "fourstroke", "urs")),
    B("Cervelo", ("cervelo",), ("=soloist", "s", "r", "caledonia", "aspero", "p")),
    B("Scott", ("scott",), ("addict", "foil", "speedster", "spark", "scale", "aspect"), domains=BIKE),
    B("Orbea", ("orbea",), ("orca", "avant", "terra", "oiz", "alma", "occam")),
    B("Felt", ("felt",), ("fr", "ar", "broam", "vr"), domains=BIKE),
    B("Factor", ("factor",), ("ostro", "=slick", "o2", "one"), domains=BIKE),
    B("Colnago", ("colnago",), ("v", "c", "g3")),
    B("Polygon", ("polygon",), ("strattos", "siskiu", "xtrada", "bend"), domains=BIKE),
    B("KTM", ("ktm",), ("macina", "revelator", "myroon"), domains=BIKE),
    B("Focus", ("focus",), ("izalco", "izalco max", "paralane", "jam", "raven", "atlas"), domains=BIKE),
    B("Look", ("look",), (), domains=BIKE),
    B("Shimano", ("shimano",), ("105", "ultegra", "dura-ace", "dura ace", "grx", "deore", "xt", "slx", "xtr")),
    B("SRAM", ("sram",), ("red", "force", "rival", "apex", "gx", "x01", "xx1")),
    B("Elitewheels", ("elitewheels", "elite wheels"), ("drive", "edge")),
    B("Winspace", ("winspace",), ("hyper", "slc", "lun")),
    # авто
    B("Teyes", ("teyes", "тейес"), ("cc3", "cc", "spro", "x1")),
    B("BBS", ("bbs",), ("lm", "rs", "ch", "fi")),
    B("Vossen", ("vossen",), ("hf", "cvt", "vfs")),
    # часы
    B("Garmin", ("garmin", "гармин"), ("fenix", "forerunner", "epix", "instinct", "venu", "enduro", "vivoactive")),
    B("Rolex", ("rolex", "ролекс"), ("submariner", "daytona", "datejust", "gmt-master", "explorer", "yacht-master"),
      synonyms={"oyster": "", "perpetual": ""}),
    B("Patek Philippe", ("patek philippe", "patek"), ("=nautilus", "=aquanaut", "=calatrava")),
    B("Audemars Piguet", ("audemars piguet",), ("=royal oak", "=royal oak offshore")),
    B("Cartier", ("cartier", "картье"), ("=santos", "=tank", "=roadster", "=ballon bleu", "=pasha")),
    B("Breitling", ("breitling",), ("navitimer", "superocean", "chronomat", "avenger")),
    B("TAG Heuer", ("tag heuer",), ("carrera", "aquaracer", "monaco", "formula")),
    B("Tudor", ("tudor",), ("black bay", "pelagos")),
    B("Longines", ("longines",), ("=hydroconquest", "=spirit", "=master collection")),
    B("Casio", ("casio", "касио", "g-shock", "g shock"), ("g-shock", "gw", "ga", "gm", "edifice", "pro trek")),
    B("Seiko", ("seiko",), ("prospex", "presage", "5", "srp", "snk")),
    B("Tissot", ("tissot",), ("=prx", "seastar", "le locle", "gentleman")),
    B("Omega", ("omega",), ("speedmaster", "seamaster", "constellation")),
    B("Citizen", ("citizen",), ("promaster", "=exceed", "eco-drive")),
    B("Hublot", ("hublot",), ("=big bang", "=king power", "classic fusion")),
    B("Richard Mille", ("richard mille",), ("rm",)),
    B("Ulysse Nardin", ("ulysse nardin",), ("=marine", "=dual time", "freak")),
    B("Zenith", ("zenith",), ("el primero", "defy", "chronomaster")),
    # оптика
    B("Levenhuk", ("levenhuk",), ("sherman", "sherman pro", "bruno", "bruno plus", "skyline", "mak", "karma", "vegas")),
    B("Sky-Watcher", ("sky-watcher", "sky watcher", "skywatcher"), ("heritage", "bk", "evostar", "skymax")),
    B("Svbony", ("svbony",), ("sv",)),
    B("Celestron", ("celestron", "селестрон"), ("nexstar", "astromaster", "powerseeker", "skymaster")),
    B("Vixen", ("vixen",), ("vmc",)),
    B("iRay", ("iray",), ("rico", "zoom", "tube", "bolt")),
    B("HikMicro", ("hikmicro", "hik micro"), ("lynx", "thunder", "gryphon", "falcon", "condor")),
    B("Pulsar", ("pulsar",), ("helion", "axion", "thermion", "merger"), domains=('optics',)),
    # Hi-Fi
    B("Sennheiser", ("sennheiser",), ("hd", "ie", "momentum")),
    B("Moondrop", ("moondrop",), ("blessing", "aria", "kato", "variations")),
    B("FiiO", ("fiio",), ("m", "k", "btr", "q")),
    # коляски
    B("Doona", ("doona",), ("=x", "=+")),
    B("Bugaboo", ("bugaboo",), ("fox", "=dragonfly", "=butterfly", "cameleon", "donkey", "bee")),
    B("Cybex", ("cybex",), ("priam", "mios", "balios", "=eezy s twist", "gazelle", "e-priam")),
    B("Anex", ("anex",), ("=eli", "m/type", "l/type", "=air-x", "e/type")),
    B("Joolz", ("joolz",), ("=day+", "=aer", "hub")),
    B("Babyzen", ("babyzen", "yoyo"), ("yoyo",)),
    # одежда (модель — только известная линейка; бренд целиком товаром не становится)
    B("Stone Island", ("stone island", "stoneisland", "стон айленд", "стоник")),
    B("Rick Owens", ("rick owens", "рик оуэнс", "drkshdw"), ("=geobasket", "=ramones", "=kiss boots")),
    B("Chrome Hearts", ("chrome hearts", "хром хартс"), ()),
    B("Prada", ("prada", "прада"), ("=america's cup", "=americas cup", "=re-nylon")),
    B("Miu Miu", ("miu miu", "miumiu", "миу миу"), ()),
    B("Maison Margiela", ("maison margiela", "margiela", "маржела", "mm6"), ("=tabi", "=replica", "=gat")),
    B("Arc'teryx", ("arc'teryx", "arcteryx", "arc teryx", "артерикс", "арктерикс"),
      ("=beta lt", "=beta ar", "=beta sl", "=alpha sv", "=atom lt", "=atom hoody", "=gamma mx",
       "=cerium lt")),  # fmt: skip
    B("Moncler", ("moncler", "монклер"), ("=maya", "=montclar", "=cardere")),
    B("C.P. Company", ("cp company", "c p company", "cpcompany", "си пи компани")),
    B("Acne Studios", ("acne studios", "acne", "акне"), (), domains=FASHION),
    B("Fear of God", ("fear of god", "essentials", "fog"), (), domains=FASHION),
    B("Represent", ("represent",), (), domains=FASHION),
    B("Balenciaga", ("balenciaga", "баленсиага"), ("=triple s", "=track", "=defender", "=speed", "=runner")),
    B("Gucci", ("gucci", "гуччи"), ("=ace", "=rhyton", "=jackie", "=dionysus", "=horsebit")),
    B("Louis Vuitton", ("louis vuitton", "луи виттон", "lv"), ("=keepall", "=neverfull", "=speedy", "=trainer"),
      domains=FASHION),
    B("Supreme", ("supreme",), domains=FASHION),
    B("Nike", ("nike", "найк"), ("air force", "air max", "air jordan", "jordan", "dunk", "=dunk low", "=sb dunk")),
    B("New Balance", ("new balance", "nb"), ("=990", "=2002r", "=550", "=9060", "=530", "=1906r"), domains=FASHION),
)

# Код модели без названия бренда, однозначно указывающий бренд (DDJ-FLX4 → Pioneer; RTX 4090 → NVIDIA).
CODE_BRANDS: dict[str, str] = {"ddj": "Pioneer", "xdj": "Pioneer", "cdj": "Pioneer", "djm": "Pioneer",
                               "redmibook": "Xiaomi", "osmo": "DJI"}  # fmt: skip
PC_CODE_BRANDS: dict[str, str] = {"rtx": "NVIDIA", "gtx": "NVIDIA", "geforce": "NVIDIA", "rx": "AMD", "radeon": "AMD"}
NOISE = {"оригинал", "оригинальный", "новый", "новая", "новое", "новые", "новинка", "бу", "б/у", "original", "new",
         "official", "в", "наличии", "dj", "из", "китая"}  # между брендом и моделью — пропустить (не больше 3)
NUMERIC_MODEL_DOMAINS = ("phone", "laptop", "pc")  # «Xiaomi 14», «Honor 16»; в одежде число — размер

ROMAN = {"ii", "iii", "iv", "v", "vi", "vii"}
MODS = {"pro", "max", "ultra", "plus", "se", "mini", "air", "lite", "ti", "super", "s", "x", "xl", "neo", "premium",
        "disc", "oled", "slim", "go", "evo", "gen", "edge", "fold", "flip"}  # fmt: skip
COLORS = {"black", "white", "silver", "gold", "blue", "red", "green", "grey", "gray", "pink", "purple", "orange",
          "черный", "белый", "серый", "синий", "красный", "зеленый", "золотой", "серебристый", "розовый"}  # fmt: skip
VARIANT_RE = re.compile(
    r"^(\d+(gb|гб|tb|тб|mb|мб)|\d+(/\d+)+(gb|гб|tb|тб|ah|ач)?|\d+([.,]\d+)?(v|в|ah|ач|w|вт|mah|мач|kw|квт|hz|гц|мм|mm|см|cm|кг|kg|л)"
    r"|(19|20)\d\d|\d+\"|\d+k)$"
)
LAPTOP_SPEC_RE = re.compile(
    r"^(i[3579]|i[3579]-?\d{4,5}[a-z]*|\d{3,5}h[xsk]?|u[3579]|ultra\d?|ryzen|rtx\d*|gtx\d*|core|intel|amd|\d{3,4}ti|rx\d*"
    r"|ssd|hdd|ips|oled|fhd|qhd|uhd|4k|2k|\d+(gb|гб|tb|тб))$"
)
MACHINE_TYPE_RE = re.compile(r"^(\d{2}[a-z]{3,4}\d{1,2}|[a-z]{3}-[a-z]{1,3}\d{1,3}[a-z]?)$")  # 16IRX9, BRN-F56
HOMOGLYPH = str.maketrans("аеорсухкмтвнАЕОРСУХКМТВН", "aeopcyxkmtbhAEOPCYXKMTBH")
SPLIT_RE = re.compile(
    r"^([a-z]+\d+|\d+)(ii|iii|iv|vi|vii|v|pro|max|ultra|plus|ti|super|se|mini|lite)$|^([a-z]+\d+)(x|s)$"
)
CYR = re.compile(r"[а-я]")
LAT = re.compile(r"[a-z]")


@dataclass(frozen=True)
class ProductIdentity:
    brand: str | None
    model: str | None
    variant: str | None
    canonical_key: str | None
    display_name: str | None
    confidence: str  # HIGH | MEDIUM | LOW | UNKNOWN
    evidence: tuple[str, ...] = ()

    @property
    def clusterable(self) -> bool:
        return self.confidence in CLUSTERABLE and self.canonical_key is not None


DOMAIN_MARKERS = (
    ("noutbuki", "laptop"), ("komplektuyuschie", "pc"), ("velosipedy", "bike"), ("elektrovelosipedy", "ebike"),
    ("samokaty", "ebike"), ("odezhda", "fashion"), ("obuv", "fashion"), ("sumki", "fashion"), ("binokli", "optics"),
    ("ohota", "optics"), ("mobile", "phone"), ("iphone", "phone"), ("planshety", "phone"), ("chasy", "watch"),
    ("fototehnika", "photo"), ("cifrovye_kamery", "photo"), ("plenocnye_kamery", "photo"), ("eksn_kamery", "action"),
    ("muzykalnye_instrumenty", "music"), ("igrovye_pristavki", "console"), ("dzhoystiki", "console"),
    ("zapchasti_i_aksessuary", "auto"), ("detskie_kolyaski", "stroller"), ("naushniki", "hifi"),
    ("usiliteli", "hifi"),
)  # fmt: skip  # порядок важен: elektrovelosipedy раньше velosipedy не нужен — проверяется первым совпавшим ниже


def domain_of(category_url: str | None) -> str:
    """Тип рынка по адресу категории — для правил разбора (у ноутбука RTX — характеристика, у видеокарты — модель)
    и для двусмысленных брендов (Trek — велосипед, а не «Pro Trek» у часов)."""
    u = category_url or ""
    if "elektrovelosipedy" in u:
        return "ebike"
    for marker, domain in DOMAIN_MARKERS:
        if marker in u:
            return domain
    return "generic"


# --- нормализация ---


def _plain(word: str) -> str:
    """Как normalize для одного слова: без диакритики (й → и), чтобы списки слов совпадали с токенами."""
    w = unicodedata.normalize("NFKD", word.casefold().replace("ё", "е"))
    return "".join(ch for ch in w if not unicodedata.combining(ch))


COLORS = {_plain(w) for w in COLORS}
NOISE = {_plain(w) for w in NOISE}


def _fix_token(tok: str) -> str:
    if CYR.search(tok) and LAT.search(tok):  # «kugoо» с кириллической «о»: смешанный токен → латиница
        tok = tok.translate(HOMOGLYPH).lower()
    return tok


def normalize(title: str) -> list[str]:
    """Токены заголовка: регистр, ё/е, кавычки, тире, мусор; запятая — отдельный токен (граница модели)."""
    t = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", title).casefold().replace("ё", "е"))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))  # Cervélo = cervelo
    t = re.sub(r"[’‘`´]", "'", t)
    t = re.sub(r"[‐‑‒–—―]", "-", t)
    t = re.sub(r"(?<=\d)[хx×*](?=\d)", "x", t)  # 12х45, 8*20 → 12x45, 8x20 (без пробелов: «360 x3» не трогать)
    t = re.sub(r"(?<=[a-z]):(?=\d)", "", t)  # c:62 → c62
    t = re.sub(r"\bgr\s?(i{2,3})(x?)\b", r"gr \1 \2", t)  # Ricoh GRIII → gr iii; GR IIIx → gr iii x
    t = re.sub(r"(?<=\d)mk(?=\d|i)", " mk", t)  # SL-1200MK7 → sl-1200 mk7
    t = re.sub(r"\binsta\s?360\s?(?=[a-z])", "insta360 ", t)  # insta 360x5, insta360go3s
    t = re.sub(r"\b(\d{2})(r\d)\b", r"\1 \2", t)  # Alienware 15r3 = 15 r3
    t = re.sub(r"\bmk\s?(\d|i{1,3}|iv)\b", r"mk\1", t)
    t = re.sub(r"(?<=[a-z0-9])\+(?=\s|$)", " plus", t)  # V3PRO+ = V3 Pro Plus
    t = re.sub(r",", " , ", t)
    t = "".join(ch if ch.isalnum() or ch in " -+/.,'&\"" else " " for ch in t)
    out: list[str] = []
    for raw in t.split():
        tok = _fix_token(raw.strip(".'"))
        if not tok:
            continue
        if tok == ",":
            out.append(",")
            continue
        whole = "/" in tok or MACHINE_TYPE_RE.match(tok) or re.fullmatch(r"\d+-\d+", tok)  # 16/512 xdj-rx3 67-01
        for part in ([tok] if whole else tok.split("-")):  # ddj-flx4 = ddj flx4 (ключ всё равно через «-»)
            m = None if VARIANT_RE.match(part) else SPLIT_RE.match(part)  # 48v — вольты, не «48 V»
            if m:  # a7iv → a7 iv, 4070ti → 4070 ti; «7x», «9s» (fenix 7x) остаются целыми
                out.extend(g for g in m.groups() if g)
            elif part:
                out.append(part)
    return out


# --- бренд ---

_ALIASES: list[tuple[tuple[str, ...], Brand]] = sorted(
    ((tuple(normalize(a)), b) for b in BRANDS for a in b.aliases), key=lambda x: -len(x[0])
)
_BY_NAME = {b.name: b for b in BRANDS}


def _find_brand(tokens: list[str], domain: str) -> tuple[Brand, int, int] | None:
    """(бренд, начало, конец алиаса): первый по позиции; на одной позиции — самый длинный алиас. Однозначный код
    модели (DDJ-…) тоже даёт бренд, модель тогда начинается с него. В домене pc видеокарта важнее бренда платы."""
    if domain == "pc":
        for i, tok in enumerate(tokens):
            if tok in PC_CODE_BRANDS:
                return _BY_NAME[PC_CODE_BRANDS[tok]], i, i
    for i, tok in enumerate(tokens):
        for alias, brand in _ALIASES:
            if brand.domains and domain not in brand.domains:
                continue  # «Pro Trek» у часов, «giant case» у ПК — не велосипедные бренды
            if tuple(tokens[i : i + len(alias)]) == alias:
                return brand, i, i + len(alias)
        if tok.split("-")[0] in CODE_BRANDS:
            return _BY_NAME[CODE_BRANDS[tok.split("-")[0]]], i, i
    return None


# --- модель ---


def _families(brand: Brand) -> list[tuple[tuple[str, ...], bool]]:
    """[(токены серии, полное ли имя)], длинные первыми."""
    fams = [(tuple(normalize(f.lstrip("="))), f.startswith("=")) for f in brand.families]
    return sorted(fams, key=lambda x: -len(x[0]))


def _is_variant(tok: str, domain: str) -> bool:
    if VARIANT_RE.match(tok) or tok in COLORS or tok in {"64", "128", "256", "512", "1024", "2048"}:
        return True
    if domain == "laptop" and LAPTOP_SPEC_RE.match(tok):
        return True
    if domain == "laptop" and tok.isdigit() and len(tok) == 4:
        return True  # 1660, 3060 в названии ноутбука — видеокарта, не модель
    if domain == "ebike" and re.fullmatch(r"\d+a", tok):
        return True  # «Wenbox U5 80A» — ток контроллера, не модель
    return domain == "bike" and tok.isdigit() and 44 <= int(tok) <= 64  # размер рамы


def _code(tok: str) -> bool:
    return any(ch.isdigit() for ch in tok) and len(tok) <= 12 and not tok.startswith("+")


def identify(title: str, domain: str = "generic") -> ProductIdentity:
    tokens = normalize(title)
    if domain == "phone" and "," in tokens:
        tokens = tokens[: tokens.index(",")]  # «iPhone 15 Pro Max, 256 ГБ, eSIM»: модель до запятой
    found = _find_brand(tokens, domain)
    if found is None:
        return ProductIdentity(None, None, None, None, None, "UNKNOWN", ("бренд не найден",))
    brand, b_start, b_end = found
    evidence = [f"бренд: {brand.name}"]
    fams = _families(brand)
    model: list[str] = []
    complete = has_code = has_family = mod_in_family = False
    j = b_end
    alias = tuple(tokens[b_start:b_end])
    implied = brand.implied.get(" ".join(alias))
    if implied:  # «iphone 15»: алиас уже серия; «=steam deck» — полное имя
        model.extend(implied.lstrip("=").split())
        complete = has_family = implied.startswith("=")
        evidence.append(f"серия из алиаса: {implied.lstrip('=')}")
    tokens = [brand.synonyms.get(t, t) for t in tokens]
    tokens = tokens[:j] + [t for t in tokens[j:] if t != ""]
    if not model:  # «Pioneer оригинал DDJ-FLX4», «Pioneer DJ контроллер DDJ FLX4»: пропустить до 3 слов-шумов,
        k = j  # но только если сразу за ними код или известная серия («Stone Island куртка 52» — не модель)
        while k < len(tokens) and k - j < 3 and (tokens[k] in NOISE or CYR.search(tokens[k])):
            k += 1
        at_k = tokens[k] if k < len(tokens) else ""
        starts_model = any(tuple(tokens[k : k + len(f)]) == f for f, _ in fams) or (
            _code(at_k) and not _is_variant(at_k, domain)
        )
        if k > j and starts_model:
            j = k
    while j < len(tokens) and len(model) < 6:
        tok = tokens[j]
        if tok == "," or _is_variant(tok, domain):
            break
        fam = next(((f, c) for f, c in fams if tuple(tokens[j : j + len(f)]) == f), None)
        if fam and (not model or not has_code or len(fam[0][0]) >= 3):  # Alienware 16 Aurora ≠ 16 Area 51
            model.extend(fam[0])
            complete = complete or fam[1]
            has_family = True
            has_code = has_code or any(_code(t) for t in fam[0])  # серия с цифрой (Teyes CC3) — уже код
            mod_in_family = True  # «Ace Pro» — Pro часть серии, код после неё (Ace Pro 2) — модель
            evidence.append(f"серия: {' '.join(fam[0])}")
            j += len(fam[0])
            continue
        if _code(tok):
            if domain == "laptop" and MACHINE_TYPE_RE.match(tok):
                break
            if model and model[-1] in MODS and not mod_in_family:
                break  # «Mini 4 Pro RC2», «Kirin V3 Pro A7»: код после Pro — комплект/мусор, не модель
            if tok.isdigit() and model and model[-1].isdigit():
                break  # «Ace Pro 2 2 батареи»: два числа подряд — второе не модель
            if tok.isdigit() and complete and domain == "fashion":
                break  # Keepall 50 — размер
            model.append(tok)
            has_code = True
            j += 1
            continue
        nxt = tokens[j + 1] if j + 1 < len(tokens) else ""
        short = tok not in MODS and re.fullmatch(r"[a-z]{2,3}", tok)
        if model and short and _code(nxt) and not _is_variant(nxt, domain):
            model.append(tok)
            j += 1
            continue
        if model and (tok in ROMAN or tok in MODS or re.fullmatch(r"mk\w+", tok)):
            model.append(tok)
            mod_in_family = False
            has_code = has_code or tok in ROMAN or tok.startswith("mk")  # GR III, MK7 — поколение
            j += 1
            continue
        if not model and tok in CODE_BRANDS and CODE_BRANDS[tok] == brand.name:
            model.append(tok)
            j += 1
            continue
        break
    while model and model[-1] in MODS and not has_code and not complete:
        model.pop()  # «Lenovo Legion Pro» без номера — не модель
    variant_tokens = [t for t in tokens[j:] if t != "," and _is_variant(t, domain)]
    machine = next((t for t in tokens[j:] if domain == "laptop" and MACHINE_TYPE_RE.match(t)), None)
    if domain == "laptop" and machine:
        variant_tokens.insert(0, machine)
    variant = " ".join(variant_tokens) or None
    model = _sony_roman(brand, model)
    if brand.name == "GoPro" and model and model[0].isdigit():  # «Go pro 13», «GoPro 12 Hero Black» = Hero 13 / 12
        model = ["hero", model[0], *[t for t in model[1:] if t != "hero"]]
        has_family = True
    model = _join_letter_number(model)
    if not model or not (has_code or complete):
        why = "модель без номера/кода" if model else "модель не найдена"
        return ProductIdentity(brand.name, None, variant, None, None, "LOW", (*evidence, why))
    pure_numbers = not has_family and all(t.isdigit() or t in MODS or t in ROMAN for t in model)
    if pure_numbers and not complete and domain not in NUMERIC_MODEL_DOMAINS:
        return ProductIdentity(brand.name, None, variant, None, None, "LOW", (*evidence, "только число — не модель"))
    confidence = "HIGH" if has_code and not pure_numbers else "MEDIUM"
    if domain == "laptop" and machine:
        evidence.append(f"тип машины: {machine}")
    key = f"{_slug(brand.name)}|{'-'.join(model)}" + (f"|{machine}" if domain == "laptop" and machine else "")
    return ProductIdentity(brand.name, _pretty(brand, model), variant, key, f"{brand.name} {_pretty(brand, model)}",
                           confidence, tuple(evidence))  # fmt: skip


def _sony_roman(brand: Brand, model: list[str]) -> list[str]:
    """Sony A7 4 = A7 IV (арабская цифра поколения после кода камеры)."""
    if brand.name == "Sony" and len(model) >= 2 and re.fullmatch(r"a[79][rsc]?|a[79]", model[-2]):
        arabic = {"2": "ii", "3": "iii", "4": "iv", "5": "v"}
        if model[-1] in arabic:
            return [*model[:-1], arabic[model[-1]]]
    return model


def _join_letter_number(model: list[str]) -> list[str]:
    """«x 16» = «x16», «x t5» = «xt5»: одиночная буква + код внутри модели — один код."""
    out: list[str] = []
    for tok in model:
        if out and len(out[-1]) == 1 and out[-1].isalpha() and re.fullmatch(r"[a-z]?\d+[a-z]?", tok):
            out[-1] += tok
        else:
            out.append(tok)
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


DISPLAY = {"iphone": "iPhone", "ipad": "iPad", "macbook": "MacBook", "airpods": "AirPods", "magicbook": "MagicBook",
           "matebook": "MateBook", "matepad": "MatePad", "redmibook": "RedmiBook", "thinkpad": "ThinkPad",
           "ideapad": "IdeaPad", "thinkbook": "ThinkBook", "zenbook": "ZenBook", "vivobook": "VivoBook",
           "proart": "ProArt", "playstation": "PlayStation", "supersix": "SuperSix", "teammachine": "Teammachine",
           "s-works": "S-Works", "g-shock": "G-Shock"}  # fmt: skip
ACRONYMS = {"rog", "tuf", "loq", "ddj", "xdj", "cdj", "djm", "rtx", "gtx", "fpv", "gfx", "tcr", "caad", "bmc"}


def _pretty(brand: Brand, model: list[str]) -> str:
    words = []
    for tok in model:
        if tok in DISPLAY:
            words.append(DISPLAY[tok])
        elif tok in MODS and tok not in {"se", "xl"} and not tok.isdigit():
            words.append(tok.capitalize())
        elif tok in ROMAN or _code(tok) or len(tok) <= 2 or tok in ACRONYMS or tok in {"se", "xl"}:
            words.append(tok.upper())
        else:
            words.append(tok.capitalize())
    text = re.sub(r"(\d)X(\d)", r"\1x\2", " ".join(words))  # 12x50
    # DDJ FLX4 → DDJ-FLX4; Fujifilm X100 V → X100V
    text = re.sub(r"^(DDJ|XDJ|CDJ|DJM|SL|RM|RG) (?=[A-Z0-9])", r"\1-", text)
    if brand.name == "Fujifilm":
        text = re.sub(r"(\d) (V|VI)$", r"\1\2", text)
    return text


def goofish_query_for(identity: ProductIdentity) -> str | None:
    """Запрос для goofish из бренда и модели (без мусора заголовка); None — модель не распознана."""
    return identity.display_name if identity.clusterable else None
