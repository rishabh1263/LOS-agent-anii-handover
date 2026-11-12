import "@testing-library/jest-dom/vitest";

// jsdom has no scrolling, no media queries and no ResizeObserver (Mantine asks for both)
window.scrollTo = () => {};
Element.prototype.scrollIntoView = () => {};
window.matchMedia = window.matchMedia || ((query: string) => ({
  matches: false, media: query, onchange: null, addListener: () => {}, removeListener: () => {},
  addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia;
class NoResize { observe() {} unobserve() {} disconnect() {} }
(window as unknown as { ResizeObserver: unknown }).ResizeObserver = window.ResizeObserver || NoResize;

// a slow machine: give async queries time (the checks themselves are unchanged)
import { configure } from "@testing-library/react";
configure({ asyncUtilTimeout: 5000 });
