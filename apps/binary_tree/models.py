from django.db import models


class BinaryTreeEdge(models.Model):
    """Closure table for the binary tree (see SPEC.md Scale Architecture).

    One row per (ancestor, descendant) pair reachable through the placement
    tree, so ancestor/descendant lookups are O(depth) reads instead of a
    recursive walk. `leg` records which of the ancestor's two legs the
    descendant falls under -- propagated down from the descendant's direct
    parent, since going through that parent still counts as the same leg
    from every ancestor above it.
    """

    class Leg(models.TextChoices):
        LEFT = "L", "Left"
        RIGHT = "R", "Right"

    ancestor = models.ForeignKey(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="descendant_edges",
    )
    descendant = models.ForeignKey(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="ancestor_edges",
    )
    depth = models.PositiveIntegerField()
    leg = models.CharField(max_length=1, choices=Leg.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ancestor", "descendant"], name="unique_binary_tree_edge"
            ),
            models.CheckConstraint(
                check=~models.Q(ancestor=models.F("descendant")),
                name="binary_tree_edge_no_self_reference",
            ),
            models.CheckConstraint(
                check=models.Q(depth__gte=1),
                name="binary_tree_edge_depth_gte_1",
            ),
        ]
        indexes = [
            # `descendant` alone doesn't need an explicit index -- ForeignKey
            # already indexes it by default. This composite covers spillover
            # lookups ("descendants of this ancestor in this leg") that the
            # single-column FK index on `ancestor` alone can't serve.
            models.Index(fields=["ancestor", "leg"]),
        ]

    def __str__(self):
        return (
            f"BinaryTreeEdge<{self.ancestor_id} -> {self.descendant_id} "
            f"depth={self.depth} leg={self.leg}>"
        )
