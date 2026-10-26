import "@testing-library/jest-dom/vitest";


// jsdom has no top-layer dialog implementation; browser smoke checks the native one.
HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
