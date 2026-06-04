import { useState } from "react";
import { thumbnailUrl } from "../api/endpoints";

/** Video thumbnail with graceful fallback (placeholder when none / load error).
 * Only requests the image when `has` is true to avoid 404 spam in lists. */
export function Thumb({
  videoId,
  has,
  size = "row",
  alt,
}: {
  videoId: number;
  has: boolean;
  size?: "lg" | "sm" | "row";
  alt?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!has || failed) {
    return <div className={`thumb thumb-ph ${size}`}>no image</div>;
  }
  return (
    <img
      className={`thumb ${size}`}
      src={thumbnailUrl(videoId)}
      alt={alt ?? "thumbnail"}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
