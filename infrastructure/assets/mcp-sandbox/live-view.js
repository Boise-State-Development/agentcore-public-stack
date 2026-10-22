/**
 * Browser sign-in live view — DCV client for a browser takeover.
 *
 * `docs/specs/authenticated-web-assessment.md` D3. Plain ES2017, no framework,
 * no build step, no third-party code: `window.dcv` comes from the Amazon DCV
 * Web Client SDK, fetched and signature-verified at build time by
 * `scripts/build/fetch-dcv-sdk.sh`.
 *
 * Trust model, which is the whole reason this file is not in the SPA
 * ------------------------------------------------------------------
 * 1. **It never calls app-api.** The SPA mints the short-lived live-view URL
 *    and posts it in. app-api's CORS is a credentialed allowlist, so letting
 *    this page call it would mean allowlisting an origin that also serves
 *    untrusted MCP App HTML — handing every App a path to the user's session.
 * 2. **It only accepts a URL from the origin that framed it.** Checked against
 *    `document.referrer`'s origin, so a sibling frame cannot feed it a stream.
 * 3. **It stores nothing.** No storage, no cookies. The signed URL lives in a
 *    closure for as long as it is valid and is replaced when the SPA posts a
 *    fresh one.
 *
 * The SigV4 query parameters
 * --------------------------
 * The live-view URL is SigV4 *query*-signed. The SDK forwards those parameters
 * onto its own requests — including the WebSocket upgrade — through the
 * `httpExtraSearchParams` callback. Without it the socket opens unsigned and
 * the service rejects it. `connect` needs it just as much as `authenticate`
 * does: the WS transport reads it directly when building the URI.
 */
(function () {
  'use strict';

  var CONNECT_MESSAGE = 'browser-live-view/connect';
  var READY_MESSAGE = 'browser-live-view/ready';
  var DISPLAY_ID = 'display';

  var statusEl = document.getElementById('status');
  var connection = null;
  var currentUrl = null;
  // Set the instant a connection attempt BEGINS, not when it completes.
  // The parent posts the URL up to three times (after minting, on the
  // iframe's `load`, and in reply to our `ready`) because it cannot know
  // which arrives first. Guarding on `connection` alone is not enough: it is
  // assigned only after `dcv.connect()` resolves, so two posts milliseconds
  // apart both reach `dcv.authenticate` and the second socket's open closes
  // the first — surfacing as `Close received after close` and auth code 10.
  var starting = false;
  // Set once `authenticate` has handed us a session. The SDK closes the auth
  // WebSocket as soon as it is done, and reports that close through the
  // `error` callback EVEN THOUGH authentication succeeded — measured on dev:
  // `connect` is already running when `dcv.authenticate failed {code: 10}`
  // arrives, and the stream then establishes normally. In Node the same close
  // is a clean code 1000. Without this flag the error handler paints
  // "Could not start the session" over a working stream, because #status is
  // an absolutely-positioned overlay covering the whole display.
  var authenticated = false;

  function setStatus(text) {
    if (!statusEl) return;
    if (text) {
      statusEl.textContent = text;
      statusEl.hidden = false;
    } else {
      statusEl.hidden = true;
    }
  }

  /**
   * The origin allowed to drive this page: the document that framed us.
   *
   * A top-level visit has no parent to trust and renders nothing — that is a
   * refusal, not a failure mode to work around.
   */
  function parentOrigin() {
    if (window.parent === window) return null;
    try {
      return document.referrer ? new URL(document.referrer).origin : null;
    } catch (e) {
      return null;
    }
  }

  function isConnectMessage(data) {
    return (
      data &&
      typeof data === 'object' &&
      data.type === CONNECT_MESSAGE &&
      typeof data.url === 'string' &&
      data.url.length > 0 &&
      data.viewport &&
      typeof data.viewport.width === 'number' &&
      typeof data.viewport.height === 'number' &&
      data.viewport.width > 0 &&
      data.viewport.height > 0
    );
  }

  /** Forward the signed URL's query parameters onto the SDK's own requests. */
  function extraSearchParamsFor(signedUrl) {
    return function () {
      try {
        return new URL(signedUrl).searchParams;
      } catch (e) {
        return new URLSearchParams();
      }
    };
  }

  function start(signedUrl, viewport) {
    // A duplicate post, or a re-mint for a still-live session: keep the
    // fresher URL for any later use, but never open a second stream. Tearing
    // a live one down mid-sign-in to reconnect with a newer signature would
    // be the opposite of what re-minting is for.
    if (starting || connection) {
      currentUrl = signedUrl;
      return;
    }
    currentUrl = signedUrl;
    starting = true;

    if (!window.dcv) {
      setStatus('The viewer failed to load.');
      return;
    }

    setStatus('Connecting…');
    dcv.setLogLevel(dcv.LogLevel.WARN);

    var extras = extraSearchParamsFor(signedUrl);

    // Strip the query before handing the URL to the SDK. It APPENDS
    // `httpExtraSearchParams` to whatever URL it is given, so passing the
    // signed URL with its query still attached sends every SigV4 parameter
    // twice and the service answers 403 — in the browser, a WebSocket that
    // opens and immediately closes ("Close received after close"), surfacing
    // as auth code 10. `connect` below has always stripped it; `authenticate`
    // did not, which is why the viewer never streamed.
    var endpoint;
    try {
      endpoint = new URL(signedUrl);
      endpoint.search = '';
      endpoint = endpoint.toString();
    } catch (e) {
      starting = false;
      setStatus('The session address was not usable.');
      return;
    }

    dcv.authenticate(endpoint, {
      // AgentCore's presigned URL *is* the credential, so there is never an
      // interactive prompt. Supplying a no-op is required — the SDK refuses a
      // configuration with a missing auth callback.
      promptCredentials: function () {},
      error: function (_auth, error) {
        // A failure reported AFTER we already have a session is the auth
        // socket closing behind a successful handshake, not a failure to
        // reach the service. Surfacing it would hide a live stream.
        if (authenticated) {
          if (window.console) console.info('dcv auth socket closed', error);
          return;
        }
        starting = false;
        setStatus('Could not start the session. It may have ended.');
        if (window.console) console.error('dcv.authenticate failed', error);
      },
      success: function (_auth, result) {
        var first = (result && result[0]) || {};
        if (!first.sessionId || !first.authToken) {
          starting = false;
          setStatus('The session could not be opened.');
          return;
        }
        authenticated = true;
        connect(first.sessionId, first.authToken, viewport, extras);
      },
      httpExtraSearchParams: extras,
    });
  }

/**
   * Pin the remote display to the browser session's real viewport.
   *
   * Mismatched dimensions are what crop or letterbox the stream, which is why
   * the viewport travels on the event rather than being a constant in the
   * frontend.
   *
   * Called from `firstFrame`, NOT from `connect().then()`. The display channel
   * is not up when the connect promise resolves, so calling it there rejects
   * with "Display channel is not available" — and because it returns a PROMISE,
   * a try/catch around it never saw the failure; it surfaced as an unhandled
   * rejection in the console instead. Measured on dev.
   */
  function applyDisplayLayout(conn, viewport) {
    if (!conn || typeof conn.requestDisplayLayout !== 'function') return;
    try {
      var result = conn.requestDisplayLayout([
        {
          name: 'Main Display',
          rect: { x: 0, y: 0, width: viewport.width, height: viewport.height },
          primary: true,
        },
      ]);
      // Not fatal either way: the stream renders at whatever the server chose.
      if (result && typeof result.catch === 'function') {
        result.catch(function (e) {
          if (window.console) console.info('requestDisplayLayout declined', e);
        });
      }
    } catch (e) {
      if (window.console) console.info('requestDisplayLayout threw', e);
    }
  }

  function connect(sessionId, authToken, viewport, extras) {
    // The query is supplied through `httpExtraSearchParams`, so strip it here
    // rather than sending it twice — the transport appends to whatever URL it
    // is given.
    var base;
    try {
      base = new URL(currentUrl);
      base.search = '';
    } catch (e) {
      starting = false;
      setStatus('The session address was not usable.');
      return;
    }

    dcv
      .connect({
        url: base.toString(),
        sessionId: sessionId,
        authToken: authToken,
        divId: DISPLAY_ID,
        // `baseUrl` is deliberately NOT set: the SDK defaults it to `dcvjs`
        // relative to this page, and the fetch script extracts to exactly that
        // name so the default resolves.
        // `httpExtraSearchParams` MUST live inside `observers` here. Passed
        // at the top level it is silently ignored by `connect` — measured
        // against the shipped bundle: top-level yields
        // `/live-view/ws` with NO query at all, so the stream socket opens
        // UNSIGNED and the service refuses it, while `observers` yields the
        // signed URI. (`authenticate` is the opposite: it reads the callback
        // from the top level. The two entry points differ.) This is also what
        // AWS's own BrowserLiveView component does.
        //
        // Callback names are exactly these — no `on` prefix. The SDK invokes
        // each with the connection as its FIRST argument, so the original
        // arguments follow it. They go in `observers` too: it is observers or
        // callbacks, never both.
        observers: {
          httpExtraSearchParams: extras,
          firstFrame: function (conn) {
            setStatus('');
            applyDisplayLayout(conn, viewport);
          },
          disconnect: function (_conn, reason) {
            connection = null;
            starting = false;
            authenticated = false;
            setStatus('The sign-in session ended.');
            if (window.console) console.info('dcv disconnected', reason);
          },
        },
      })
      .then(function (conn) {
        connection = conn;
      })
      .catch(function (error) {
        starting = false;
        setStatus('Could not connect to the sign-in session.');
        if (window.console) console.error('dcv.connect failed', error);
      });
  }

  var allowed = parentOrigin();
  if (!allowed) {
    setStatus('This viewer must be opened from the app.');
    return;
  }

  window.addEventListener('message', function (event) {
    if (event.origin !== allowed) return;
    if (!isConnectMessage(event.data)) return;
    start(event.data.url, event.data.viewport);
  });

  // The SPA also posts on the iframe's `load` event; this covers the race
  // where that fires before this script has attached its listener.
  window.parent.postMessage({ type: READY_MESSAGE }, allowed);
})();
