export async function fetchCsrfToken(apiBaseUrl: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/api/v1/auth/csrf`, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new Error(
      "Could not initialize CSRF protection. Check that the API URL is reachable and that the frontend origin is allowed by CORS."
    );
  }

  const body = (await response.json().catch(() => ({}))) as { csrf_token?: unknown; detail?: unknown };
  if (!response.ok || typeof body.csrf_token !== "string" || body.csrf_token.length < 32) {
    const detail = typeof body.detail === "string" ? body.detail : "Invalid CSRF endpoint response";
    throw new Error(`Could not initialize CSRF protection: ${detail}`);
  }
  return body.csrf_token;
}

export function explainApiError(status: number, detail: unknown, fallback: string): string {
  const message = typeof detail === "string" ? detail : "";
  if (status === 403 && /csrf/i.test(message)) {
    return "CSRF validation failed. The browser may not be sending the CSRF cookie, or its value does not match the request header. For local development, open http://localhost:3000 (not 127.0.0.1), set NEXT_PUBLIC_API_URL=http://localhost:8000, keep APP_ENV=development and SESSION_COOKIE_SAMESITE=lax, and ensure CORS_ORIGINS=http://localhost:3000. For hosted deployments, use a same-site custom domain/reverse proxy; third-party cookies between separate Vercel/Railway domains may be blocked.";
  }
  if (status === 429) {
    return "Too many attempts in a short period. Wait a little before trying again.";
  }
  if (status === 503 && /rate-limit service/i.test(message)) {
    return "Security checks are temporarily unavailable. Please try again shortly.";
  }
  if (status === 401) {
    // Login returns 401 for incorrect owner or member credentials.
    // Do not mislabel every 401 as an expired browser session.
    if (/invalid email or password/i.test(message)) {
      return "Invalid email or password. Check the email and password. For the configured owner, confirm APP_ADMIN_EMAIL and APP_ADMIN_PASSWORD_HASH; invited members should use the password set when they accepted their invitation.";
    }
    if (/login required|session.*(missing|expired)|(missing|expired).*session/i.test(message)) {
      return "Your session is missing or expired. Refresh the page and sign in again.";
    }
    return message || fallback;
  }
  return message || fallback;
}
