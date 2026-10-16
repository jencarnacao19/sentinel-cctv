import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

// jsdom implements neither matchMedia nor the observers and pointer APIs Radix
// relies on. Without these the dropdown and switch primitives throw on mount,
// which would make every test that renders a camera row fail for reasons that
// have nothing to do with the camera row.
beforeEach(() => {
  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
      }),
    })
  }

  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  }

  if (!window.DOMRect) {
    // Radix measures its trigger before positioning a portal.
    window.DOMRect = class {
      constructor(
        public x = 0,
        public y = 0,
        public width = 0,
        public height = 0,
      ) {}
      top = 0
      left = 0
      right = 0
      bottom = 0
      toJSON() {
        return {}
      }
      static fromRect() {
        return new window.DOMRect()
      }
    }
  }

  // Pointer capture and scrollIntoView are unimplemented in jsdom; Radix calls
  // both while opening a menu.
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false
    Element.prototype.setPointerCapture = () => {}
    Element.prototype.releasePointerCapture = () => {}
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {}
  }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
