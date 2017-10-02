from collections import OrderedDict
from logging import getLogger

from lazy import lazy

from courseware.model_data import ScoresClient
from lms.djangoapps.grades.config import assume_zero_if_absent, should_persist_grades
from lms.djangoapps.grades.models import PersistentSubsectionGrade
from lms.djangoapps.grades.scores import possibly_scored
from openedx.core.lib.grade_utils import is_score_higher_or_equal
from student.models import anonymous_id_for_user
from submissions import api as submissions_api

from .course_data import CourseData
from .subsection_grade import SubsectionGrade, ZeroSubsectionGrade

log = getLogger(__name__)


class SubsectionGradeFactory(object):
    """
    Factory for Subsection Grades.
    """
    def __init__(self, student, course=None, course_structure=None, course_data=None):
        self.student = student
        self.course_data = course_data or CourseData(student, course=course, structure=course_structure)

        self._cached_subsection_grades = None
        self._unsaved_subsection_grades = OrderedDict()

    def create(self, subsection, read_only=False):
        """
        Returns the SubsectionGrade object for the student and subsection.

        If read_only is True, doesn't save any updates to the grades.
        """
        self._log_event(
            log.debug, u"create, read_only: {0}, subsection: {1}".format(read_only, subsection.location), subsection,
        )

        subsection_grade = self._get_bulk_cached_grade(subsection)
        if not subsection_grade:
            if assume_zero_if_absent(self.course_data.course_key):
                subsection_grade = ZeroSubsectionGrade(subsection, self.course_data)
            else:
                subsection_grade = SubsectionGrade.create(
                    subsection, self.course_data.structure, self._submissions_scores, self._csm_scores,
                )
                if should_persist_grades(self.course_data.course_key):
                    if read_only:
                        self._unsaved_subsection_grades[subsection_grade.location] = subsection_grade
                    else:
                        grade_model = subsection_grade.update_or_create_model(self.student)
                        self._update_saved_subsection_grade(subsection.location, grade_model)
        return subsection_grade

    def bulk_create_unsaved(self):
        """
        Bulk creates all the unsaved subsection_grades to this point.
        """
        SubsectionGrade.bulk_create_models(
            self.student, self._unsaved_subsection_grades.values(), self.course_data.course_key
        )
        self._unsaved_subsection_grades.clear()

    def update(self, subsection, only_if_higher=None, score_deleted=False):
        """
        Updates the SubsectionGrade object for the student and subsection.
        """
        self._log_event(log.debug, u"update, subsection: {}".format(subsection.location), subsection)

        calculated_grade = SubsectionGrade.create(
            subsection, self.course_data.structure, self._submissions_scores, self._csm_scores,
        )

        if should_persist_grades(self.course_data.course_key):
            if only_if_higher:
                try:
                    grade_model = PersistentSubsectionGrade.read_grade(self.student.id, subsection.location)
                except PersistentSubsectionGrade.DoesNotExist:
                    pass
                else:
                    orig_subsection_grade = SubsectionGrade.read(
                        subsection, grade_model, self.course_data.structure, self._submissions_scores, self._csm_scores,
                    )
                    if not is_score_higher_or_equal(
                            orig_subsection_grade.graded_total.earned,
                            orig_subsection_grade.graded_total.possible,
                            calculated_grade.graded_total.earned,
                            calculated_grade.graded_total.possible,
                    ):
                        return orig_subsection_grade

            grade_model = calculated_grade.update_or_create_model(self.student, score_deleted)
            self._update_saved_subsection_grade(subsection.location, grade_model)

        return calculated_grade

    @lazy
    def _csm_scores(self):
        """
        Lazily queries and returns all the scores stored in the user
        state (in CSM) for the course, while caching the result.
        """
        scorable_locations = [block_key for block_key in self.course_data.structure if possibly_scored(block_key)]
        return ScoresClient.create_for_locations(self.course_data.course_key, self.student.id, scorable_locations)

    @lazy
    def _submissions_scores(self):
        """
        Lazily queries and returns the scores stored by the
        Submissions API for the course, while caching the result.
        """
        anonymous_user_id = anonymous_id_for_user(self.student, self.course_data.course_key)
        return submissions_api.get_scores(str(self.course_data.course_key), anonymous_user_id)

    def _get_bulk_cached_grade(self, subsection):
        """
        Returns the student's SubsectionGrade for the subsection,
        while caching the results of a bulk retrieval for the
        course, for future access of other subsections.
        Returns None if not found.
        """
        if should_persist_grades(self.course_data.course_key):
            saved_subsection_grades = self._get_bulk_cached_subsection_grades()
            grade = saved_subsection_grades.get(subsection.location)
            if grade:
                return SubsectionGrade.read(
                    subsection, grade, self.course_data.structure, self._submissions_scores, self._csm_scores,
                )

    def _get_bulk_cached_subsection_grades(self):
        """
        Returns and caches (for future access) the results of
        a bulk retrieval of all subsection grades in the course.
        """
        if self._cached_subsection_grades is None:
            self._cached_subsection_grades = {
                record.full_usage_key: record
                for record in PersistentSubsectionGrade.bulk_read_grades(self.student.id, self.course_data.course_key)
            }
        return self._cached_subsection_grades

    def _update_saved_subsection_grade(self, subsection_usage_key, subsection_model):
        """
        Updates (or adds) the subsection grade for the given
        subsection usage key in the local cache, iff the cache
        is populated.
        """
        if self._cached_subsection_grades is not None:
            self._cached_subsection_grades[subsection_usage_key] = subsection_model

    def _log_event(self, log_func, log_statement, subsection):
        """
        Logs the given statement, for this instance.
        """
        log_func(u"Grades: SGF.{}, course: {}, version: {}, edit: {}, user: {}".format(
            log_statement,
            self.course_data.course_key,
            getattr(subsection, 'course_version', None),
            getattr(subsection, 'subtree_edited_on', None),
            self.student.id,
        ))
