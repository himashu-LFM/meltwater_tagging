// Shared Supabase auth helper. Expects window.__SUPABASE_URL__ / __SUPABASE_ANON_KEY__
// to be set by the page template. Exposes window.Auth with session + fetch helpers.

const Auth = (() => {
  let client = null;
  let session = null;

  function init() {
    if (!window.supabase) {
      console.error("Supabase JS SDK failed to load.");
      return;
    }
    client = window.supabase.createClient(window.__SUPABASE_URL__, window.__SUPABASE_ANON_KEY__);
  }

  async function getSession() {
    if (!client) init();
    const { data } = await client.auth.getSession();
    session = data.session;
    return session;
  }

  async function signUp(email, password) {
    if (!client) init();
    return client.auth.signUp({ email, password });
  }

  async function signIn(email, password) {
    if (!client) init();
    const r = await client.auth.signInWithPassword({ email, password });
    if (!r.error) session = r.data.session;
    return r;
  }

  async function signOut() {
    if (!client) init();
    await client.auth.signOut();
    session = null;
  }

  // Redirect to /login if there's no active session. Call at the top of
  // protected pages.
  async function requireAuthOrRedirect() {
    const s = await getSession();
    if (!s) { window.location.href = "/login"; return null; }
    return s;
  }

  // fetch() wrapper that attaches the Supabase access token automatically.
  async function authedFetch(url, options = {}) {
    const s = await getSession();
    const headers = Object.assign({}, options.headers || {}, {
      Authorization: `Bearer ${s ? s.access_token : ""}`,
    });
    return fetch(url, Object.assign({}, options, { headers }));
  }

  function userEmail() {
    return session && session.user ? session.user.email : "";
  }

  init();
  return { getSession, signUp, signIn, signOut, requireAuthOrRedirect, authedFetch, userEmail };
})();
