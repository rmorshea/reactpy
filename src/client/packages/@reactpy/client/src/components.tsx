import React, {
  createElement,
  createContext,
  useState,
  useRef,
  useContext,
  useEffect,
  Fragment,
  MutableRefObject,
  ChangeEvent,
} from "react";
// @ts-ignore
import { get as getJsonPointer } from "json-pointer";
import logger from "./logger";
import {
  ReactPyVdom,
  ReactPyComponent,
  createChildrenFromVdom,
  createChildrenFromArray,
  createAttributes,
  loadImportSource,
  ImportSourceBinding,
} from "./reactpy-vdom";
import { ReactPyClient } from "./reactpy-client";

const ClientContext = createContext<ReactPyClient>(null as any);

export function Layout(props: { client: ReactPyClient }): JSX.Element {
  const currentModel: ReactPyVdom = useState({ tagName: "" })[0];
  const forceUpdate = useForceUpdate();

  useEffect(
    () =>
      props.client.onMessage("layout-update", ({ path, model }) => {
        // we must copy because we mutate the model (i.e. ReactPyVdom.updateModel)
        const modelCopy = structuredClone(model);
        if (path === "") {
          Object.assign(currentModel, modelCopy);
          forceUpdate();
        } else {
          const oldModel: ReactPyVdom = getJsonPointer(currentModel, path);
          oldModel.updateModel
            ? oldModel.updateModel(modelCopy)
            : logger.error("No updateModel function on model", oldModel);
        }
      }),
    [currentModel, props.client],
  );

  return (
    <ClientContext.Provider value={props.client}>
      <Element model={currentModel} />
    </ClientContext.Provider>
  );
}

export function InnerLayout(props: {
  client: ReactPyClient;
  children: ReactPyVdom[];
}): JSX.Element {
  return (
    <ClientContext.Provider value={props.client}>
      {createChildrenFromArray(props.children, (child) => (
        <Element model={child} key={child.key} />
      ))}
    </ClientContext.Provider>
  );
}

export function Element({ model }: { model: ReactPyVdom }): JSX.Element | null {
  const forceUpdate = useForceUpdate();

  useEffect(() => {
    const updateCallback = (newModel: ReactPyVdom) => {
      // We mutate the model to avoid keeping the old version in memory
      Object.assign(model, newModel);
      forceUpdate();
    };
    model.updateModel = updateCallback;
    return () => {
      if (model.updateModel === updateCallback) {
        delete model.updateModel;
      }
    };
  });

  if (model.error !== undefined) {
    if (model.error) {
      return <pre>{model.error}</pre>;
    } else {
      return null;
    }
  }

  let SpecializedElement: ReactPyComponent;
  if (model.tagName in SPECIAL_ELEMENTS) {
    SpecializedElement =
      SPECIAL_ELEMENTS[model.tagName as keyof typeof SPECIAL_ELEMENTS];
  } else if (model.importSource) {
    SpecializedElement = ImportedElement;
  } else {
    SpecializedElement = StandardElement;
  }

  return <SpecializedElement model={model} />;
}

function StandardElement({ model }: { model: ReactPyVdom }) {
  const client = React.useContext(ClientContext);
  // Use createElement here to avoid warning about variable numbers of children not
  // having keys. Warning about this must now be the responsibility of the client
  // providing the models instead of the client rendering them.
  return createElement(
    model.tagName === "" ? Fragment : model.tagName,
    createAttributes(model, client),
    ...createChildrenFromVdom(model, (child) => {
      return <Element model={child} key={child.key} />;
    }),
  );
}

function UserInputElement({ model }: { model: ReactPyVdom }): JSX.Element {
  const client = useContext(ClientContext);
  const props = createAttributes(model, client);
  const [value, setValue] = React.useState(props.value);

  // honor changes to value from the client via props
  React.useEffect(() => setValue(props.value), [props.value]);

  const givenOnChange = props.onChange;
  if (typeof givenOnChange === "function") {
    props.onChange = (event: ChangeEvent<any>) => {
      // immediately update the value to give the user feedback
      setValue(event.target.value);
      // allow the client to respond (and possibly change the value)
      givenOnChange(event);
    };
  }

  // Use createElement here to avoid warning about variable numbers of children not
  // having keys. Warning about this must now be the responsibility of the client
  // providing the models instead of the client rendering them.
  return createElement(
    model.tagName,
    // overwrite
    { ...props, value },
    ...createChildrenFromVdom(model, (child) => (
      <Element model={child} key={child.key} />
    )),
  );
}

function ScriptElement({ model }: { model: ReactPyVdom }) {
  const ref = useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!ref.current) {
      return;
    }
    const scriptContent = model?.children?.filter(
      (value): value is string => typeof value == "string",
    )[0];

    let scriptElement: HTMLScriptElement;
    if (model.attributes) {
      scriptElement = document.createElement("script");
      for (const [k, v] of Object.entries(model.attributes)) {
        scriptElement.setAttribute(k, v);
      }
      if (scriptContent) {
        scriptElement.appendChild(document.createTextNode(scriptContent));
      }
      ref.current.appendChild(scriptElement);
    } else if (scriptContent) {
      const scriptResult = eval(scriptContent);
      if (typeof scriptResult == "function") {
        return scriptResult();
      }
    }
  }, [model.key, ref.current]);

  return <div ref={ref} />;
}

function ImportedElement({ model }: { model: ReactPyVdom }) {
  const importSourceVdom = model.importSource;
  const importSourceRef = useImportSource(model);

  if (!importSourceVdom) {
    return null;
  }

  const importSourceFallback = importSourceVdom.fallback;

  if (!importSourceVdom) {
    // display a fallback if one was given
    if (!importSourceFallback) {
      return null;
    } else if (typeof importSourceFallback === "string") {
      return <span>{importSourceFallback}</span>;
    } else {
      return <StandardElement model={importSourceFallback} />;
    }
  } else {
    return <span ref={importSourceRef} />;
  }
}

function useForceUpdate() {
  const [, setState] = useState(false);
  return () => setState((old) => !old);
}

function useImportSource(model: ReactPyVdom): MutableRefObject<any> {
  const vdomImportSource = model.importSource;

  const mountPoint = useRef<HTMLElement>(null);
  const client = React.useContext(ClientContext);
  const [binding, setBinding] = useState<ImportSourceBinding | null>(null);

  React.useEffect(() => {
    let unmounted = false;

    if (vdomImportSource) {
      loadImportSource(vdomImportSource, client).then((bind) => {
        if (!unmounted && mountPoint.current) {
          setBinding(bind(mountPoint.current));
        }
      });
    }

    return () => {
      unmounted = true;
      if (
        binding &&
        vdomImportSource &&
        !vdomImportSource.unmountBeforeUpdate
      ) {
        binding.unmount();
      }
    };
  }, [client, vdomImportSource, setBinding, mountPoint.current]);

  // this effect must run every time in case the model has changed
  useEffect(() => {
    if (!(binding && vdomImportSource)) {
      return;
    }
    binding.render(model);
    if (vdomImportSource.unmountBeforeUpdate) {
      return binding.unmount;
    }
  });

  return mountPoint;
}

const SPECIAL_ELEMENTS = {
  input: UserInputElement,
  script: ScriptElement,
  select: UserInputElement,
  textarea: UserInputElement,
};
