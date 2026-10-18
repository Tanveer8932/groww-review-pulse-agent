# SYNTHETIC TEST FIXTURE — not real review data
"""Hand-written language-evaluation sentences for the Phase 2 language policy.

These sentences were written for testing. They imitate the style of app-store reviews
(finance jargon, acronyms, informal grammar, Hinglish) but none is a real Groww review, and
none may ever be imported, analysed, or quoted. They are used only to measure the English
filter's behaviour and to document its tradeoffs (docs/implementation-plan.md, Phase 2).

Each list is labelled by the expected decision:

* ``FINANCE_ENGLISH``: English with finance terms; must be kept.
* ``GENERAL_ENGLISH``: everyday English app-review text; must be kept.
* ``HINGLISH``: romanized Hindi or Hindi-English code-mixing; must be excluded.
* ``OTHER_LANGUAGES``: non-English Latin-script text; must be excluded.
* ``BORDERLINE_ENGLISH``: very short or list-like English that the policy may exclude; the
  tests only document the outcome, they do not require it to be kept.
"""

from __future__ import annotations

FINANCE_ENGLISH: list[str] = [
    "Nice app for SIP and stocks, the charts are clean and fast to load",
    "KYC got rejected twice without any reason, please fix the verification process",
    "IPO allotment status is never updated on time in the app, very disappointing",
    "Withdrawal takes 3 days to reach my bank account, other brokers do it same day",
    "Good platform for mutual funds and SIP, but the UI for F&O trading is confusing",
    "Brokerage charges are hidden, DP charges were deducted without any notice",
    "Unable to sell my ETF units, order rejected with RMS error again and again",
    "Best app for beginners to start investing in stocks and mutual funds",
    "Stocks SIP feature is great but the NAV updates are delayed by a day",
    "My UPI mandate for SIP failed and now my SIP got cancelled automatically",
    "GTT orders are not triggering properly, lost money because of this bug",
    "Excellent app, smooth UI, easy KYC, fast IPO application and zero AMC",
    "Intraday MTF margin calculation is wrong and the pledge option takes forever",
    "Customer care is useless, no reply on my ticket for withdrawals since last week",
    "Very bad experience, money debited but SIP instalment shows failed",
    "Groww is simple to use for mutual funds and stocks, I recommend it",
    "Option chain loads slowly and the Greeks are missing, need improvement",
    "Please add more indicators in charts and allow basket orders for F&O",
    "Applied for IPO through UPI but the mandate request never came to my bank app",
    "Mutual fund redemption amount was credited after five working days",
    "Demat account opening was quick and the KYC was done within a day",
    "Trading in futures and options is smooth but the brokerage is a bit high",
    "Stop loss order was not executed and I lost a lot of money in intraday",
    "The app shows wrong average price for my stocks after the bonus issue",
    "SIP date cannot be changed once it is started, please add this option",
    "Love the simple design, investing in index funds has never been this easy",
    "Withdrawal is instant now, very happy with the recent update and support",
    "Portfolio returns are calculated incorrectly, XIRR does not match my statement",
    "Fund transfer from bank failed but money got deducted from my account",
    "Good app for long term investing, but the research reports are very basic",
    "Charges for delivery trades are too high compared to other discount brokers",
    "KYC verification is stuck for two weeks and nobody from support responds",
    "Smooth experience for stocks and mutual funds, the watchlist is very useful",
    "Please improve the screener and add fundamental ratios for every company",
    "Pledge for margin is not working and the collateral value shows zero",
]

GENERAL_ENGLISH: list[str] = [
    "The app keeps crashing whenever I try to place an order in the morning",
    "Worst app ever. Money deducted but order not placed, support is useless",
    "Login OTP not coming since 3 days, please resolve asap my account is blocked",
    "Very good app for beginners, easy to use and a clean interface for mutual funds",
    "The app crashes when I open my portfolio during market hours, please fix",
    "Login OTP not received, app stuck on loading screen since morning",
    "After the latest update the app is very slow and the screen freezes often",
    "Dark mode is great but the font is too small on my phone, please increase it",
    "Notifications come late, I missed my alerts because of this issue",
    "Simple and clean app, everything works fine and the support team is helpful",
    "I cannot log in after changing my phone, the app says device not supported",
    "The new design is confusing and it takes too many taps to find my holdings",
    "Worst customer service I have ever seen, they keep closing my complaint",
    "Very useful app, I check my investments every day and it never fails",
    "App is good overall but sometimes the charts do not load on mobile data",
]

HINGLISH: list[str] = [
    "App acha hai but withdrawal me bahut time lagta hai",
    "SIP ka paisa kat gaya lekin units nahi mile",
    "Bhai ye app bekar hai, KYC pending hai 10 din se",
    "Customer care se baat karo to koi solution nahi milta",
    "Order place nahi ho raha market open hote hi app hang ho jata hai",
    "IPO apply kiya tha par paise abhi tak refund nahi hue",
    "Bahut accha app hai beginners ke liye, easy to use",
    "Mera account block ho gaya bina reason ke, please help",
    "Stocks kharidne me problem aa rahi hai, app crash ho raha",
    "Paisa withdraw karne me 3 din lagte hai, ye galat hai",
    "App theek hai lekin charges zyada lagte hain",
    "Kal se login nahi ho raha, OTP nahi aa raha",
    "Yeh app bahut accha hai lekin withdrawal mein bahut time lagta hai yaar",
    "App bahut slow hai and customer care koi response nahi deta hai",
    "Paise kat gaye but order place nahi hua please help karo",
    "Ekdum mast app hai, sab kuch easy hai aur fast bhi",
    "Kyc kab tak complete hoga, 5 din ho gaye abhi tak pending",
    "Market khulte hi app band ho jata hai, bahut loss hua",
    "Support wale sirf ticket close kar dete hai, problem solve nahi karte",
    "Mutual fund me invest karna bahut aasan hai is app se",
    "Refund kab milega, paisa account se kat gaya tha",
    "App update ke baad chart dikh hi nahi raha",
    "Sab theek tha par ab app khul hi nahi rahi",
    "Brokerage bahut jyada hai, dusre app me kam lagta hai",
    "Itna slow app maine kabhi nahi dekha, sudharo isko",
    "Mujhe samajh nahi aa raha ki SIP kaise cancel kare",
    "Accha app hai lekin kabhi kabhi hang hota hai",
    "Demat account khulwane me koi dikkat nahi hui, sahi hai",
    "Withdrawal ka paisa abhi tak bank me nahi aaya",
    "Bekar app, koi bhi kaam time pe nahi hota",
]

#: Hinglish with at most one marker word, so rule 2 does not fire and the detector and the
#: lexical rescue decide. These are the cases a lower threshold would put at risk.
HINGLISH_LOW_MARKER: list[str] = [
    "Shandaar experience, ekdam smooth trading, dil khush ho gaya Groww team",
    "Dhanyavaad Groww team, bohot badhiya kaam, investment ekdam asaan",
    "Zabardast app, ekdam badhiya service and support bhi mast",
    "Itna slow app maine kabhi nahi dekha, sudharo isko",
    "Mast trading experience, ho gaya profit, shukriya Groww team members",
    "Fatafat KYC ho gayi, investment shuru, tension khatam, superb app experience",
]

OTHER_LANGUAGES: list[str] = [
    "Aplikasi ini sangat bagus untuk investasi saham dan reksa dana",
    "Aplikasi ini sangat membantu untuk pemula yang ingin belajar investasi",
    "Este aplicativo é muito bom para investir em ações",
    "Muy buena aplicación para invertir en fondos mutuos",
    "Ang app na ito ay napakaganda para sa pag-invest",
    "Cette application est excellente pour investir en bourse",
    "Die App ist sehr gut zum Investieren in Aktien und Fonds",
    "Esta aplicación es muy buena para invertir y fácil de usar cada día",
    "Saya suka aplikasi ini karena mudah digunakan dan cepat",
    "Napakabagal ng app na ito tuwing umaga kapag bukas ang merkado",
]

BORDERLINE_ENGLISH: list[str] = [
    "good app",
    "Nice",
    "SIP IPO KYC",
    "Excellent excellent excellent",
]
