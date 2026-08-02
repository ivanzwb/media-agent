import axios from "axios";

// Single axios instance for all backend calls. Base is empty so paths are
// same-origin (dev: Vite proxy; prod: served by FastAPI).
export const api = axios.create({ baseURL: "" });

/** POST a plain object as multipart/form-data (matches FastAPI Form(...)).
 *
 * File/Blob values are appended as raw file parts (needed by upload endpoints
 * such as /api/draft/{id}/mermaid-image); everything else is stringified.
 */
export async function postForm<T = any>(
  url: string,
  data: Record<string, string | number | boolean | undefined | null | File | Blob> = {}
): Promise<T> {
  const fd = new FormData();
  for (const [k, v] of Object.entries(data)) {
    if (v === undefined || v === null) continue;
    if (v instanceof Blob) {
      // The 3-arg append gives the part a filename so FastAPI parses it as an
      // UploadFile (a nameless Blob part arrives as plain FormData instead).
      const name = v instanceof File && v.name ? v.name : "file";
      fd.append(k, v, name);
    } else {
      fd.append(k, String(v));
    }
  }
  const r = await api.post(url, fd);
  return r.data as T;
}

/** GET JSON. */
export async function getJson<T = any>(url: string, params?: object): Promise<T> {
  const r = await api.get(url, { params });
  return r.data as T;
}
