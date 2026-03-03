import { ArticleStatus } from "../components/StatusBadge";

export interface Article {
  id: number;
  status: ArticleStatus;
  contentMode: "rss" | "full" | "translated";
  score: number;
  title: string;
  ruTitle?: string;
  subtitle?: string;
  ruSummary?: string;
  source: string;
  sourceUrl: string;
  publishedDate: string;
  fullText?: string;
  fullTextRu?: string;
  imagePrompt?: string;
  imageUrl?: string;
  scheduleDate?: string;
  feedback?: string;
}

export interface Source {
  id: number;
  active: boolean;
  kind: "rss" | "html";
  rank: number;
  name: string;
  url: string;
  articlesCount: number;
  latestPublished: string;
}

export interface ScoreParameter {
  id: number;
  key: string;
  title: string;
  weight: number;
  description: string;
  influenceRule: string;
  active: boolean;
}

export const mockArticles: Article[] = [
  {
    id: 1,
    status: "selected_hourly",
    contentMode: "translated",
    score: 8.7,
    title: "OpenAI Announces GPT-5 with Revolutionary Reasoning Capabilities",
    ruTitle: "OpenAI анонсировала GPT-5 с революционными возможностями рассуждений",
    subtitle: "The new model shows unprecedented performance in complex reasoning tasks",
    ruSummary: "Новая модель демонстрирует беспрецедентную производительность в сложных задачах рассуждений. Ожидается выход в Q2 2026 года.",
    source: "TechCrunch",
    sourceUrl: "https://techcrunch.com",
    publishedDate: "2026-03-02T10:30:00Z",
    fullText: "OpenAI has announced the development of GPT-5, marking a significant leap forward in artificial intelligence capabilities. The new model demonstrates unprecedented performance in complex reasoning tasks, with particular improvements in mathematical problem-solving, code generation, and multi-step logical inference...",
    imagePrompt: "futuristic AI brain with glowing neural connections, digital art, blue and purple tones",
  },
  {
    id: 2,
    status: "ready",
    contentMode: "full",
    score: 7.3,
    title: "Google DeepMind Unveils AlphaCode 3: AI That Writes Enterprise Software",
    ruTitle: "Google DeepMind представила AlphaCode 3: ИИ для написания корпоративного ПО",
    subtitle: "New system can generate full-stack applications with minimal guidance",
    ruSummary: "Новая система может генерировать полноценные приложения с минимальными указаниями",
    source: "The Verge",
    sourceUrl: "https://theverge.com",
    publishedDate: "2026-03-02T09:15:00Z",
    imageUrl: "https://images.unsplash.com/photo-1555949963-aa79dcee981c?w=800",
  },
  {
    id: 3,
    status: "scored",
    contentMode: "full",
    score: 6.8,
    title: "Meta's New Llama 4 Model Challenges Proprietary AI Giants",
    source: "VentureBeat",
    sourceUrl: "https://venturebeat.com",
    publishedDate: "2026-03-02T08:00:00Z",
    subtitle: "Open-source model achieves competitive performance at lower cost",
  },
  {
    id: 4,
    status: "review",
    contentMode: "rss",
    score: 5.2,
    title: "Microsoft Integrates AI Agents into Windows 12",
    source: "Ars Technica",
    sourceUrl: "https://arstechnica.com",
    publishedDate: "2026-03-01T16:45:00Z",
    subtitle: "Personal AI assistants will be built directly into the operating system",
  },
  {
    id: 5,
    status: "new",
    contentMode: "rss",
    score: 4.5,
    title: "Anthropic Releases Claude 4 with Enhanced Safety Features",
    source: "MIT Technology Review",
    sourceUrl: "https://technologyreview.com",
    publishedDate: "2026-03-01T14:20:00Z",
  },
  {
    id: 6,
    status: "published",
    contentMode: "translated",
    score: 9.1,
    title: "Stanford Researchers Develop AI That Can Predict Protein Folding in Seconds",
    ruTitle: "Исследователи Стэнфорда разработали ИИ для предсказания сворачивания белков за секунды",
    source: "Nature",
    sourceUrl: "https://nature.com",
    publishedDate: "2026-03-01T12:00:00Z",
    imageUrl: "https://images.unsplash.com/photo-1532187863486-abf9dbad1b69?w=800",
  },
  {
    id: 7,
    status: "inbox",
    contentMode: "rss",
    score: 3.9,
    title: "AI Startup Raises $200M for Next-Gen Language Models",
    source: "Bloomberg",
    sourceUrl: "https://bloomberg.com",
    publishedDate: "2026-03-01T10:30:00Z",
  },
  {
    id: 8,
    status: "double",
    contentMode: "rss",
    score: 2.1,
    title: "OpenAI GPT-5 Launch Details Revealed",
    source: "TechRadar",
    sourceUrl: "https://techradar.com",
    publishedDate: "2026-03-02T11:00:00Z",
  },
  {
    id: 9,
    status: "archived",
    contentMode: "full",
    score: 7.8,
    title: "EU Passes Comprehensive AI Regulation Framework",
    source: "Politico",
    sourceUrl: "https://politico.eu",
    publishedDate: "2026-02-28T15:00:00Z",
  },
  {
    id: 10,
    status: "rejected",
    contentMode: "rss",
    score: 1.8,
    title: "Top 10 AI Tools for Small Business",
    source: "Forbes",
    sourceUrl: "https://forbes.com",
    publishedDate: "2026-02-28T09:00:00Z",
  },
];

export const mockSources: Source[] = [
  {
    id: 1,
    active: true,
    kind: "rss",
    rank: 1,
    name: "TechCrunch AI",
    url: "https://techcrunch.com/category/artificial-intelligence/feed/",
    articlesCount: 234,
    latestPublished: "2026-03-02T10:30:00Z",
  },
  {
    id: 2,
    active: true,
    kind: "rss",
    rank: 2,
    name: "The Verge AI",
    url: "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
    articlesCount: 189,
    latestPublished: "2026-03-02T09:15:00Z",
  },
  {
    id: 3,
    active: true,
    kind: "rss",
    rank: 3,
    name: "VentureBeat AI",
    url: "https://venturebeat.com/category/ai/feed/",
    articlesCount: 156,
    latestPublished: "2026-03-02T08:00:00Z",
  },
  {
    id: 4,
    active: true,
    kind: "html",
    rank: 4,
    name: "MIT Technology Review",
    url: "https://www.technologyreview.com/topic/artificial-intelligence/",
    articlesCount: 98,
    latestPublished: "2026-03-01T14:20:00Z",
  },
  {
    id: 5,
    active: false,
    kind: "rss",
    rank: 5,
    name: "Ars Technica AI",
    url: "https://arstechnica.com/tag/artificial-intelligence/feed/",
    articlesCount: 67,
    latestPublished: "2026-02-28T12:00:00Z",
  },
];

export const mockScoreParameters: ScoreParameter[] = [
  {
    id: 1,
    key: "source_authority",
    title: "Авторитет источника",
    weight: 2.5,
    description: "Вес публикации в зависимости от репутации источника",
    influenceRule: "rank * weight",
    active: true,
  },
  {
    id: 2,
    key: "recency",
    title: "Свежесть",
    weight: 3.0,
    description: "Бонус за недавно опубликованные материалы",
    influenceRule: "decay_function(hours_ago)",
    active: true,
  },
  {
    id: 3,
    key: "technical_depth",
    title: "Техническая глубина",
    weight: 2.0,
    description: "Наличие технических деталей и анализа",
    influenceRule: "nlp_score * weight",
    active: true,
  },
  {
    id: 4,
    key: "novelty",
    title: "Новизна",
    weight: 2.8,
    description: "Оригинальность информации",
    influenceRule: "1 - similarity_to_existing",
    active: true,
  },
  {
    id: 5,
    key: "audience_relevance",
    title: "Релевантность аудитории",
    weight: 1.5,
    description: "Соответствие интересам целевой аудитории",
    influenceRule: "embedding_similarity * weight",
    active: true,
  },
];
