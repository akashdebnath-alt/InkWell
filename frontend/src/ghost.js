import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { apiStream } from "./api.js";

const key = new PluginKey("ghost");

export const GhostText = Extension.create({
  name: "ghostText",
  addOptions() {
    return { getDocId: () => null, delay: 500 };
  },
  addProseMirrorPlugins() {
    const { getDocId, delay } = this.options;
    let timer = null;
    let controller = null;

    return [
      new Plugin({
        key,
        state: {
          init: () => ({ text: "", pos: null }),
          apply(tr, previous) {
            const meta = tr.getMeta(key);
            if (meta) return meta;
            if (tr.docChanged || tr.selectionSet) return { text: "", pos: null };
            return previous;
          },
        },
        props: {
          decorations(state) {
            const suggestion = key.getState(state);
            if (!suggestion.text) return DecorationSet.empty;
            const element = document.createElement("span");
            element.className = "ghost";
            element.textContent = suggestion.text;
            return DecorationSet.create(state.doc, [
              Decoration.widget(suggestion.pos, element, { side: 1 }),
            ]);
          },
          handleKeyDown(view, event) {
            const suggestion = key.getState(view.state);
            if (event.key === "Tab" && suggestion.text) {
              event.preventDefault();
              view.dispatch(view.state.tr.insertText(suggestion.text, suggestion.pos));
              return true;
            }
            if (event.key === "Escape" && suggestion.text) {
              view.dispatch(view.state.tr.setMeta(key, { text: "", pos: null }));
              return true;
            }
            return false;
          },
        },
        view() {
          return {
            update(view, previousState) {
              if (view.state.doc.eq(previousState.doc)) return;
              clearTimeout(timer);
              controller?.abort();

              const docId = getDocId();
              const { from, empty } = view.state.selection;
              if (!docId || !empty) return;

              const before = view.state.doc.textBetween(0, from, "\n");
              if (before.trim().length < 10) return;

              timer = setTimeout(async () => {
                const requestController = new AbortController();
                controller = requestController;
                const position = from;
                try {
                  const suggestion = await apiStream("/suggest", {
                    method: "POST",
                    body: JSON.stringify({ doc_id: docId, text_before: before }),
                    signal: requestController.signal,
                  });
                  if (
                    requestController.signal.aborted ||
                    getDocId() !== docId ||
                    view.state.selection.from !== position ||
                    !view.state.selection.empty
                  ) {
                    return;
                  }
                  const cleanedSuggestion = suggestion.trim();
                  if (cleanedSuggestion) {
                    const separator = /\s$/.test(before) ? "" : " ";
                    view.dispatch(
                      view.state.tr.setMeta(key, {
                        text: separator + cleanedSuggestion,
                        pos: position,
                      }),
                    );
                  }
                } catch (error) {
                  if (error.name !== "AbortError") {
                    window.dispatchEvent(new CustomEvent("inkwell:api-error", { detail: error }));
                  }
                }
              }, delay);
            },
            destroy() {
              clearTimeout(timer);
              controller?.abort();
            },
          };
        },
      }),
    ];
  },
});
