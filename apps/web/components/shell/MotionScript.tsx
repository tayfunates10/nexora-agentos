/**
 * Runs before the first paint and does two things no stylesheet can do on its own:
 * it marks that JavaScript is live, which is the only condition under which the
 * reveal-on-scroll rules are allowed to hide anything, and it arms the theme colour
 * transition one frame later so switching themes animates but the first render does not.
 *
 * Nothing here decides the theme; the server already rendered that attribute, so there is
 * no flash to correct and no hydration mismatch to cause.
 */
const SCRIPT = `(function(){var d=document.documentElement;try{if(!window.matchMedia||!matchMedia('(prefers-reduced-motion: reduce)').matches){d.dataset.motion='on'}}catch(e){}requestAnimationFrame(function(){d.setAttribute('data-theme-ready','')})})()`;

export function MotionScript() {
  return <script dangerouslySetInnerHTML={{ __html: SCRIPT }}/>;
}
