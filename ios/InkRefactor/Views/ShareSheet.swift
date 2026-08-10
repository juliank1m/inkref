import SwiftUI
import UIKit

/// `UIActivityViewController` is how the refactored file gets back into GoodNotes
/// (SPEC §9 step 6). On iPad it is presented as a popover, and a popover with no anchor
/// throws before it ever appears — hence the source view/rect below, which SwiftUI's
/// `.sheet` presentation does not supply for us.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let vc = UIActivityViewController(activityItems: items, applicationActivities: nil)
        anchor(vc)
        return vc
    }

    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {
        anchor(vc)      // re-anchor after a rotation or a split-view resize
    }

    private func anchor(_ vc: UIActivityViewController) {
        guard let popover = vc.popoverPresentationController else { return }
        let host = vc.view.superview ?? vc.view
        popover.sourceView = host
        popover.sourceRect = CGRect(x: host?.bounds.midX ?? 0, y: host?.bounds.midY ?? 0,
                                    width: 0, height: 0)
        popover.permittedArrowDirections = []
    }
}
