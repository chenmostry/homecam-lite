export const MAX_REMOTE_ICE_CANDIDATES = 128;

export function enqueueRemoteIce(session, candidate) {
  if (!session || session.cancelled) return false;
  if (session.pc?.remoteDescription && session.remoteReady && !session.flushingIce && !session.iceQueue.length) {
    return session.pc.addIceCandidate(candidate).then(() => true, () => false);
  }
  if (session.iceQueue.length >= MAX_REMOTE_ICE_CANDIDATES) return false;
  session.iceQueue.push(candidate);
  return true;
}

export async function flushRemoteIce(session, isCurrent = () => true) {
  if (!session || session.cancelled || !session.remoteReady || !session.pc?.remoteDescription || !isCurrent()) return;
  if (session.flushPromise) return session.flushPromise;
  session.flushingIce = true;
  session.flushPromise = (async () => {
    while (session.iceQueue.length && !session.cancelled && isCurrent() && session.pc?.remoteDescription) {
      const candidate = session.iceQueue.shift();
      try { await session.pc.addIceCandidate(candidate); } catch { /* A stale trickle candidate is skipped. */ }
    }
  })();
  try { await session.flushPromise; }
  finally {
    session.flushPromise = null;
    session.flushingIce = false;
  }
}
