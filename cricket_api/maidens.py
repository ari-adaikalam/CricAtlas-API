"""What a maiden is, written once for the scorecard and the career routes.

An over is a maiden when all of this holds for the whole over, not for one bowler's share of it:

  1. It is confirmed complete: it holds the normal number of legal balls (6, or 5 in The Hundred) and Cricsheet lists no miscount for it, or Cricsheet
     lists it as miscounted and its legal balls are the number listed. The umpire's count of a miscounted over stands (MCC Law 17.5.1), so a 7-ball or
     5-ball over the umpires counted as an over can be a maiden. An unfinished last over, a short or long over nobody listed, and an over whose listed count
     the balls contradict are not confirmed, so none of them earns a maiden.
  2. One bowler delivered every ball of it, wides and no balls included. An over shared by two bowlers, even if the second bowled only a wide, earns no maiden.
  3. No run was charged to that bowler: runs off the bat, wides and no balls. Byes, leg byes and penalty runs are not charged.

Legal-ball totals, run rate and economy stay on the balls actually bowled. This only decides which overs count as maidens.
"""

# Every maiden over as (match_id, innings, over, bowler, bowler_id). `where` limits the deliveries read, and it must keep every delivery of an over, so it may
# name matches and overs but never a bowler.
MAIDEN_OVERS = """
SELECT b.match_id, b.innings, b.over, any_value(b.bowler) AS bowler, any_value(b.bowler_id) AS bowler_id
FROM balls b JOIN matches mt ON mt.match_id = b.match_id
LEFT JOIN miscounted_overs mo ON mo.match_id = b.match_id AND mo.innings = b.innings AND mo.over = b.over
{where}
GROUP BY b.match_id, b.innings, b.over, mo.balls, mt.balls_per_over
HAVING count(DISTINCT coalesce(b.bowler_id, b.bowler)) = 1
   AND sum(coalesce(b.runs_off_bat, 0) + coalesce(b.wides, 0) + coalesce(b.noballs, 0)) = 0
   AND count(*) FILTER (WHERE b.is_legal_delivery) = coalesce(mo.balls, coalesce(mt.balls_per_over, 6))
"""
