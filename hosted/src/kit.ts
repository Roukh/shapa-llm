// The OAuth provider objects for the origin a request arrived on.

import { oauthKit, originOf, type OAuthKit } from "./oauth.js";

export function getKit(request: Request): OAuthKit {
  return oauthKit(originOf(request));
}
