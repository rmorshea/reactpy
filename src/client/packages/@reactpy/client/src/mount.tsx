import React from "react";
import { render } from "react-dom";
import { Layout, InnerLayout } from "./components";
import { ReactPyClient } from "./reactpy-client";
import { ReactPyVdom } from "./reactpy-vdom";

/**
 * Mounts the ReactPy client to the given element.
 *
 * @param element The element to mount the client to.
 * @param client The client to mount.
 * @returns Nothing.
 */
export function mount(element: HTMLElement, client: ReactPyClient): void {
  render(<Layout client={client} />, element);
}

/**
 * Mounts the ReactPy client to the given element assuming that the element in question
 * is already controled by a <Layout /> component. This typically occurs inside a
 * custom component in order to render its children.
 *
 * @param element The element to mount the client to.
 * @param client The client to mount.
 * @returns Nothing.
 */
export function mountInner(
  element: HTMLElement,
  client: ReactPyClient,
  ...children: ReactPyVdom[]
): void {
  render(<InnerLayout client={client}>{...children}</InnerLayout>, element);
}
