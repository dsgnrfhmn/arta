/*
  arta-ai-edit.js — Edit with AI, the part Portfolio and CV share: a bar under the preview head
  (dis-system/local-components/ai-edit-bar) with an input for the request and a button that sends it.

  The server makes one model call and stores the whole new set as ONE undo step; the figure in the
  confirmation popup comes from the server, never from here. The header button is magenta
  (aria-expanded) while the bar is open. The bar's button reads "Regenerate" while the set is
  exactly as the last AI edit left it: pressing it undoes that edit and runs the request again on
  the set as it was, so edits do not pile up. Any other change to the set turns it back into "Edit".

  The page keeps what is its own and hands it in:
    ids        { toggle, bar, input, run }   element ids
    api        { estimate(id, prompt), run(id, prompt), undo(id) }   each answers what the server sends
    get()      the open set (null when none)
    set(s)     take a set the server sent
    render()   draw the page again from the open set
    signature(set)   a string that changes whenever the set's content does
    confirm(title, body, okLabel, onOk)   ask the cost question and run onOk on yes
    stepUndo() the page's own Undo step (the toast's Undo button)
    toastError(message)
  attach() answers { render } : call it whenever the page is drawn (it hides the bar when there is
  nothing to edit and sets the button's label).
*/
(function () {
  function attach(opts) {
    const $ = (key) => document.getElementById(opts.ids[key]);
    const toggle = $('toggle');
    const bar = $('bar');
    let last = null; // the last AI edit: { setId, signature }

    const canRegenerate = () => {
      const set = opts.get();
      return !!set && !!last && last.setId === set.id && !!set.can_undo && last.signature === opts.signature(set);
    };

    function setOpen(open) {
      bar.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
    }

    function render() {
      const set = opts.get();
      if (!set || set.archived) setOpen(false); // nothing to edit here
      // The label sits in text-button's own span (it carries the underline): set that, not the button.
      const run = $('run');
      (run.querySelector('.ds-text-button__text') || run).textContent = canRegenerate() ? 'Regenerate' : 'Edit';
    }

    toggle.addEventListener('click', () => {
      const set = opts.get();
      if (!set) {
        window.dsToast.show({ message: 'Open or create a set first.' });
        return;
      }
      if (set.archived) {
        window.dsToast.show({ message: 'An archived set is read-only.' });
        return;
      }
      setOpen(bar.hidden);
      if (!bar.hidden) $('input').focus();
    });

    bar.addEventListener('ds-ai-edit-close', () => {
      setOpen(false);
      toggle.focus();
    });

    async function run(prompt, regenerate) {
      const controls = [$('input'), $('run')];
      bar.setAttribute('data-busy', '');
      controls.forEach((c) => { c.disabled = true; });
      const setId = opts.get().id;
      let undone = false;
      try {
        if (regenerate) {
          opts.set(await opts.api.undo(setId));
          undone = true;
        }
        const result = await opts.api.run(setId, prompt);
        opts.set(result);
        last = { setId, signature: opts.signature(result) };
        const mock = result.ai.origin === 'mock';
        if (!result.ai.changed) {
          window.dsToast.show({ message: mock ? 'Mock edit: nothing to change.' : 'The edit changed nothing.' });
        } else {
          window.dsToast.show({
            message: mock ? 'Mock edit applied: the entries are in reverse order.' : 'Edit applied.',
            actions: [{ label: 'Undo', onClick: () => opts.stepUndo() }],
          });
        }
      } catch (error) {
        if (undone) last = null;
        opts.toastError(`Could not edit with AI: ${error.message} ${undone ? 'The previous edit was undone.' : 'Nothing was changed.'}`);
      } finally {
        bar.removeAttribute('data-busy');
        controls.forEach((c) => { c.disabled = false; });
      }
      opts.render();
    }

    bar.addEventListener('ds-ai-edit-submit', async (event) => {
      const prompt = event.detail.prompt;
      const set = opts.get();
      if (!set || set.archived) return;
      if (!prompt) {
        window.dsToast.show({ message: 'Write what to change first.' });
        return;
      }
      let estimate;
      try {
        estimate = await opts.api.estimate(set.id, prompt);
      } catch (error) {
        opts.toastError(`Could not prepare the edit: ${error.message}`);
        return;
      }
      const usd = estimate.usd.toFixed(2);
      const regenerate = canRegenerate();
      const lead = regenerate ? 'The last AI edit is undone first, then the request runs again. ' : '';
      opts.confirm(
        regenerate ? 'Regenerate with AI' : 'Edit with AI',
        estimate.mocked
          ? `${lead}This call is mocked: nothing is charged, and the result is a stand-in, not a real edit. A real call would cost approximately $${usd}.`
          : `${lead}This sends the set's content to the Anthropic API and changes the set as you describe. It costs approximately $${usd}. One Undo restores the set.`,
        estimate.mocked ? (regenerate ? 'Regenerate (mock)' : 'Edit (mock)') : (regenerate ? 'Regenerate using API' : 'Edit using API'),
        () => run(prompt, regenerate)
      );
    });

    return { render };
  }

  window.ArtaAiEdit = { attach };
})();
