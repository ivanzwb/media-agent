import axios from "axios";

// Single axios instance for all backend calls. Base is empty so paths are
// same-origin (dev: Vite proxy; prod: served by FastAPI).
export const api = axios.create({ baseURL: "" });

/** POST a plain object as multipart/form-data (matches FastAPI Form(...)). */
export async function postForm<T = any>(
  url: string,
  data: Record<string, string | number | boolean | undefined | null> = {}
): Promise<T> {
  const fd = new FormData();
  for (const [k, v] of Object.entries(data)) {
    if (v !== undefined && v !== null) fd.append(k, String(v));
  }
  const r = await api.post(url, fd);
  return r.data as T;
}

/** GET JSON. */
export async function getJson<T = any>(url: string, params?: object): Promise<T> {
  const r = await api.get(url, { params });
  return r.data as T;
}
