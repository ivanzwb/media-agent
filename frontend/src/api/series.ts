// A series is read on two pages — the run in progress and the library of past
// ones — so its shape and its labels live in one place.

export interface SeriesChapter {
  id: number;
  order: number;
  title: string;
  scope: string;
  status: string;
  error: string | null;
  draft_id: number | null;
  prerequisites: string[];
  search_queries: string[];
  preflight_hits: number | null;
}

export interface Series {
  id: number;
  title: string;
  topic: string;
  parts: number;
  ref_count: number;
  status: string;
  error: string | null;
  knowledge_map: string;
  chapters: SeriesChapter[];
  chapters_done: number;
  chapters_failed: number;
  created_at: string;
}

export const CHAPTER_STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: "待开始", color: "default" },
  researching: { label: "检索资料", color: "processing" },
  writing: { label: "写作中", color: "processing" },
  done: { label: "已完成", color: "success" },
  failed: { label: "未完成", color: "error" },
  cancelled: { label: "已取消", color: "warning" },
};

export const SERIES_STATUS: Record<string, { label: string; color: string }> = {
  planned: { label: "待写作", color: "blue" },
  running: { label: "运行中", color: "processing" },
  done: { label: "已完成", color: "success" },
  partial: { label: "部分完成", color: "warning" },
  cancelled: { label: "已取消", color: "warning" },
  failed: { label: "失败", color: "error" },
};

export function exportUrl(seriesId: number, format: "md" | "html") {
  return `/api/series/${seriesId}/export?format=${format}`;
}
