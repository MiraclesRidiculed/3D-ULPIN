export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export async function api<T>(path:string, init?:RequestInit):Promise<T> {
  const res = await fetch(`${API}${path}`, { ...init, headers:{"Content-Type":"application/json", ...(init?.headers || {})}, cache:"no-store" });
  if (!res.ok) { const detail = await res.json().catch(()=>({})); throw new Error(detail.detail || `Request failed (${res.status})`); }
  return res.json();
}
