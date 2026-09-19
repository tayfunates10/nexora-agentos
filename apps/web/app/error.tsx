"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return <main><h1>Unable to load the control plane</h1><p>Please try again.</p><button onClick={reset}>Try again</button></main>;
}
