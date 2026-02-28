SOURCES = [
    # AI-first media (your list)
    # HTML-first for sources with missing/broken/stale RSS
    ("The Rundown AI", "https://www.therundown.ai/", 1, 0.0, "html"),
    ("Superhuman AI", "https://www.superhuman.ai/", 2, 0.0, "html"),
    ("Ben's Bites", "https://bensbites.co/rss", 3, 0.0),
    ("TechCrunch AI", "https://techcrunch.com/tag/ai/feed", 4, 0.0),
    ("MarkTechPost", "https://www.marktechpost.com/feed/", 5, 0.0),
    ("Mindstream", "https://www.mindstream.news/", 6, 0.0, "html"),
    ("ArtificialIntelligence-News", "https://www.artificialintelligence-news.com/feed/", 7, 0.0),
    ("Last Week in AI", "https://lastweekin.ai/feed", 8, 0.0),
    ("Import AI (Substack)", "https://importai.substack.com/feed", 9, 0.0),
    ("Alpha Signal", "https://alphasignal.ai/rss", 10, 0.0),
    ("AI Valley", "https://aivalley.ai/", 11, 0.0, "html"),
    ("Towards AI", "https://towardsai.net/feed", 12, 0.0),
    ("VentureBeat AI", "https://venturebeat.com/category/ai", 13, 0.0, "html"),
    ("Analytics India Mag", "https://analyticsindiamag.com/feed/", 15, 0.0),
    ("TLDR AI", "https://bullrich.dev/tldr-rss/ai.rss", 16, 0.0),

    # Big tech media: keep only AI-focused feeds to reduce noise
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 20, 0.0),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss", 21, 0.0),
    ("MIT Tech Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 22, 0.0),
    ("Bloomberg Tech", "https://feeds.bloomberg.com/technology/news.rss", 23, 0.0),
    ("OpenAI News", "https://openai.com/news/rss.xml", 24, 0.0),
    ("Anthropic", "https://www.anthropic.com/news", 24, 0.0, "html"),
    ("Meta AI", "https://ai.meta.com/blog/", 25, 0.0, "html"),
    ("Microsoft AI", "https://news.microsoft.com/source/topics/ai/", 26, 0.0, "html"),
    ("Amazon AI", "https://www.aboutamazon.com/news/tag/artificial-intelligence", 27, 0.0, "html"),
    ("Tesla AI", "https://www.tesla.com/blog/", 28, 0.0, "html"),
    ("Reuters Tech", "https://www.reuters.com/technology/", 29, 0.0, "html"),
    ("Business Insider", "https://www.businessinsider.com/artificial-intelligence", 30, 0.0, "html"),
    ("Ars Technica", "https://arstechnica.com/ai/", 31, 0.0, "html"),
    ("Ars Technica AI RSS", "https://arstechnica.com/ai/feed/", 31, 0.0),
    ("NVIDIA News", "https://nvidianews.nvidia.com/rss", 32, 0.0),
    ("Shopify Dev Changelog", "https://shopify.dev/changelog/feed.xml", 33, 0.0),

    # Research / links
    ("Hugging Face", "https://huggingface.co/papers/date/{date}", 34, 0.0, "html"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", 35, 0.0),
    ("Papers with Code (latest)", "https://paperswithcode.com/latest/rss", 36, 0.0),
    ("arXiv cs.CL", "https://export.arxiv.org/rss/cs.CL", 37, 0.0),
    ("arXiv cs.LG", "https://export.arxiv.org/rss/cs.LG", 38, 0.0),
    ("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI", 39, 0.0),
    ("Hacker News (best)", "https://hnrss.org/best", 40, 0.0),
    ("Hacker News (frontpage)", "https://hnrss.org/frontpage", 41, 0.0),
    ("GitHub Trending", "https://github.com/trending?since=daily", 42, 0.0, "html"),
    ("Canva Launches", "https://www.canva.com/launches/", 43, 0.0, "html"),
    ("Supabase AI RSS", "https://zdpdvwhvukelzzbzbjvh.supabase.co/functions/v1/rss-feed", 44, 0.0),

    # Model releases via GitHub Atom (works as RSS)
    ("QwenLM releases", "https://github.com/QwenLM/Qwen/releases.atom", 45, 0.0),
    ("MistralAI releases", "https://github.com/mistralai/mistral-inference/releases.atom", 46, 0.0),
]
