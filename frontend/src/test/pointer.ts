import { fireEvent } from '@testing-library/react';

// jsdom has no PointerEvent/capture implementation. Give React and native
// listeners the same pointer fields without requiring browser layout.
export function pointer(target: HTMLElement | Window, type: string,
  fields: Partial<Pick<PointerEvent, 'pointerId' | 'clientX' | 'button' | 'isPrimary'>> = {}) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { pointerId: 1, clientX: 260, button: 0, isPrimary: true, ...fields });
  fireEvent(target, event);
}
